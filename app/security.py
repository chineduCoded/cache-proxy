import asyncio
import hmac
import time
from typing import Protocol

from fastapi import Request, Response, status
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings


def constant_time_equals(a: str, b: str) -> bool:
    """String equality that doesn't leak how many leading characters
    matched via response-timing — the naive `a != b` on a secret token
    comparison is a real (if low-severity, for a self-hosted cache-clear
    endpoint) timing side-channel.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class RateLimiter(Protocol):
    async def allow(self, key: str) -> bool: ...


class InMemoryRateLimiter:
    """Fixed-window counter, per process. Fine for a single instance;
    for multiple replicas behind a load balancer, `RedisRateLimiter`
    gives every replica a shared view instead of N independent limits
    that each let the configured rate through (N times the intended
    total, in the worst case).
    """

    def __init__(self, *, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window = window_seconds
        self._counters: dict[str, tuple[float, int]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            window_start, count = self._counters.get(key, (now, 0))
            if now - window_start >= self._window:
                window_start, count = now, 0
            count += 1
            self._counters[key] = (window_start, count)
            # Opportunistic cleanup so this dict doesn't grow without
            # bound across the life of a long-running process.
            if len(self._counters) > 10_000:
                cutoff = now - self._window
                self._counters = {
                    k: v for k, v in self._counters.items() if v[0] >= cutoff
                }
            return count <= self._max_requests


class RedisRateLimiter:
    """Fixed-window counter shared across every replica via Redis.

    Uses INCR + EXPIRE NX rather than a Lua script for simplicity; the
    tiny race between the two calls means a key can very occasionally
    live slightly longer than the window, which just makes the limit
    marginally stricter for a moment — an acceptable trade for staying
    simple.
    """

    def __init__(self, *, url: str, max_requests: int, window_seconds: float) -> None:
        from redis import asyncio as redis_asyncio

        self._redis = redis_asyncio.from_url(url, decode_responses=False)
        self._max_requests = max_requests
        self._window_seconds = window_seconds

    def _key(self, key: str) -> str:
        window = int(time.time() // self._window_seconds)
        return f"caching-proxy:ratelimit:{key}:{window}"

    async def allow(self, key: str) -> bool:
        redis_key = self._key(key)
        count = int(await self._redis.incr(redis_key))

        if count == 1:
            await self._redis.expire(redis_key, int(self._window_seconds) + 1)
        return count <= self._max_requests

def builder_rate_limiter(settings: Settings) -> RateLimiter | None:
    if not settings.rate_limit_enabled:
        return None
    if settings.cache_backend == "redis" and settings.redis_url:
        return RedisRateLimiter(
            url=settings.redis_url,
            max_requests=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
        )
    return InMemoryRateLimiter(
        max_requests=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )


def client_identifier(request: Request, *, trust_forwarded_headers: bool) -> str:
    """Best-effort caller identity for rate limiting.

    Only trusts `X-Forwarded-For` / `X-Real-IP` when the proxy is
    explicitly configured to sit behind a trusted reverse proxy /
    load balancer — otherwise any client could put an arbitrary value
    in those headers and rate-limit someone else's IP instead of their own.
    """
    if trust_forwarded_headers:
        # Prefer X-Forwarded-For (first / leftmost = original client)
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Take the leftmost non-empty entry
            client_ip = forwarded.split(",", 1)[0].strip()
            if client_ip:
                return client_ip

        # Fallback used by some single-proxy setups (nginx, etc.)
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            real_ip = real_ip.strip()
            if real_ip:
                return real_ip

    # Direct connection (or headers not trusted)
    if request.client and request.client.host:
        return request.client.host

    return "unknown"

class RateLimitMiddleware:
    """Applies the configured RateLimiter to every request, keyed by
    caller IP. Admin/health/metrics endpoints are exempt — rate-limiting
    your own health checks or the admin token holder is never the intent.
    """

    _EXEMPT_PREFIXES = ("/_health", "/_ready", "/metrics", "/_internal/")

    def __init__(self, app: ASGIApp, *, limiter: "RateLimiter") -> None:
        self.app = app
        self._limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Fast path for exempt endpoints
        path = scope.get("path", "")
        if path.startswith(self._EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Build a temporary Request only for the identifier helper
        request = Request(scope, receive)
        key = client_identifier(request, trust_forwarded_headers=True)

        if not await self._limiter.allow(key):
            response = Response(
                content='{"error": "rate limit exceeded"}',
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                media_type="application/json",
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds baseline defensive response headers. Deliberately minimal —
    this proxy forwards arbitrary origin content, so aggressive headers
    like a strict CSP would apply Content-Security-Policy rules to
    content this process doesn't control and can't reason about.
    """

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self._hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")

                if self._hsts:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        "max-age=63072000; includeSubDomains",
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)