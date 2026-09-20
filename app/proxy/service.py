import json
import logging
import time
from collections.abc import AsyncIterator

import httpx
from fastapi import Request
from starlette.responses import Response, StreamingResponse

from app.cache.backend import CacheBackend
from app.cache.entry import CacheEntry
from app.config import Settings
from app.metrics import Metrics
from app.proxy.cache_control import (
    request_forbids_cache_read,
    request_forbids_cache_write,
    response_cache_policy,
)
from app.proxy.errors import RequestBodyTooLarge
from app.proxy.headers import (
    apply_headers,
    filter_request_headers,
    filter_response_headers,
)
from app.proxy.keys import build_cache_key

logger = logging.getLogger("caching_proxy")

_CACHEABLE_METHODS = {"GET"}


class ProxyService:
    """Forwards requests to the configured origin, transparently caching
    cacheable responses according to the origin's own Cache-Control
    and Vary headers (falling back to a configured default TTL when the
    origin gives no explicit directive).

    Holds its dependencies (HTTP client, cache, settings) explicitly rather
    than reaching for module globals, so it can be constructed once per app
    (real deployment) or once per test (isolated, no shared state).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        client: httpx.AsyncClient,
        cache: CacheBackend,
        metrics: Metrics | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._cache = cache
        self._metrics = metrics

    def _target_url(self, path: str, query_string: str) -> str:
        url = f"{self._settings.origin}/{path.lstrip('/')}"
        if query_string:
            url = f"{url}?{query_string}"
        return url

    async def handle(self, request: Request) -> Response:
        method = request.method.upper()
        target_url = self._target_url(request.url.path, request.url.query)
        started = time.monotonic()
        cache_status = "BYPASS"

        try:
            # Non-cachable methods - jsut forward
            if method not in _CACHEABLE_METHODS:
                response, _, _ = await self._fetch(request, target_url, method, cache=False)
                return response

            request_headers = dict(request.headers)
            allow_read = not request_forbids_cache_read(request_headers)
            allow_write = not request_forbids_cache_write(request_headers)

            cache_key = build_cache_key(
                method=method,
                url=target_url,
                request_headers=request_headers,
                vary_headers=self._settings.cache_vary_headers,
            )

            # Fast path - no lock needed on a pure HIT
            if allow_read:
                cached = await self._cache.get(cache_key)
                if cached is not None:
                    cache_status = "HIT"
                    return self._response_from_entry(cached, cache_status=cache_status)

            # Stampede protection: only one concurrent request per cache
            # key actually reaches the origin; the rest wait and re-check.
            async with self._cache.lock_for(cache_key):
                if allow_read:
                    cached = await self._cache.get(cache_key)
                    if cached is not None:
                        cache_status = "HIT"
                        return self._response_from_entry(cached, cache_status=cache_status)

            response, entry, ttl = await self._fetch(request, target_url, method, cache=allow_write)

            if entry is not None:
                await self._cache.set(cache_key, entry, ttl_seconds=ttl)

            cache_status = "MISS"
            if isinstance(response, Response):
                response.headers["X-Cache"] = cache_status

            return response
        finally:
            if self._metrics:
                self._metrics.requests_total.labels(
                    method=method,
                    cache_status=cache_status,
                ).inc()
                self._metrics.request_duration_seconds.labels(method).observe(time.monotonic() - started)

    async def _read_body_limited(self, request: Request) -> bytes:
        limit = self._settings.max_request_body_bytes
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    raise RequestBodyTooLarge()
            except ValueError:
                pass  # malformed Content-Length — fall through to the streamed check

        total = 0
        chunks: list[bytes] = []
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise RequestBodyTooLarge()
            chunks.append(chunk)
        return b"".join(chunks)

    async def _fetch(
        self,
        request: Request,
        target_url: str,
        method: str,
        *,
        cache: bool,
    ) -> tuple[Response, CacheEntry | None, float]:
        try:
            body = await self._read_body_limited(request)
        except RequestBodyTooLarge:
            return (
                self._error_response(
                    413, "Request body exceeds the configured maximum size"
                ),
                None,
                0.0,
            )

        request_headers = filter_request_headers(list(request.headers.items()))
        stream_cm = self._client.stream(
            method,
            target_url,
            headers=dict(request_headers),
            content=body or None,
            timeout=self._settings.request_timeout_seconds,
        )
        try:
            upstream = await stream_cm.__aenter__()
        except httpx.InvalidURL as exc:
            if self._metrics:
                self._metrics.upstream_errors_total.labels(kind="invalid_url").inc()
            return self._error_response(400, f"Invalid URL: {exc}"), None, 0.0
        except httpx.TimeoutException as exc:
            logger.warning("upstream timeout method=%s url=%s: %s", method, target_url, exc)
            if self._metrics:
                self._metrics.upstream_errors_total.labels(kind="timeout").inc()
            return self._error_response(504, f"Upstream timed out: {exc}"), None, 0.0
        except httpx.RequestError as exc:
            logger.warning("upstream error method=%s url=%s: %s", method, target_url, exc)
            if self._metrics:
                self._metrics.upstream_errors_total.labels(kind="connection").inc()
            return self._error_response(502, f"Error fetching from origin: {exc}"), None, 0.0

        response_headers = filter_response_headers(list(upstream.headers.multi_items()))
        media_type = upstream.headers.get("content-type")
        status_code = upstream.status_code

        byte_iter = upstream.aiter_bytes()
        buffered = bytearray()
        exceeded = False
        try:
            async for chunk in byte_iter:
                buffered.extend(chunk)
                if len(buffered) > self._settings.max_buffered_response_bytes:
                    exceeded = True
                    break
        except httpx.RequestError as exc:
            await stream_cm.__aexit__(type(exc), exc, exc.__traceback__)
            logger.warning("upstream read error method=%s url=%s: %s", method, target_url, exc)
            if self._metrics:
                self._metrics.upstream_errors_total.labels(kind="read").inc()
            return self._error_response(502, f"Error reading origin response: {exc}"), None, 0.0

        if not exceeded:
            await stream_cm.__aexit__(None, None, None)
            content = bytes(buffered)
            response = Response(content=content, status_code=status_code, media_type=media_type)
            apply_headers(response, response_headers)

            entry: CacheEntry | None = None
            ttl = 0.0
            if cache:
                policy = response_cache_policy(
                    status_code=status_code,
                    response_headers=response_headers,
                    default_ttl_seconds=self._settings.cache_ttl_seconds,
                    configured_vary_headers=self._settings.cache_vary_headers,
                )
                if policy.cacheable and len(content) <= self._settings.cache_max_item_bytes:
                    entry = CacheEntry(
                        status_code=status_code,
                        headers=tuple(response_headers),
                        content=content,
                        media_type=media_type,
                    )
                    ttl = policy.ttl_seconds
            return response, entry, ttl

        # Too large to buffer (and therefore too large to cache): stream
        # the rest straight through to the client instead of holding the
        # whole response in memory.
        logger.info(
            "response exceeds max_buffered_response_bytes, streaming without caching "
            "method=%s url=%s",
            method,
            target_url,
        )

        async def body_iterator() -> AsyncIterator[bytes]:
            try:
                yield bytes(buffered)
                async for chunk_byte in byte_iter:
                    yield chunk_byte
            finally:
                await stream_cm.__aexit__(None, None, None)

        response = StreamingResponse(
            body_iterator(), status_code=status_code, media_type=media_type
        )
        apply_headers(response, response_headers)
        return response, None, 0.0

    def _is_cacheable(self, method: str, upstream: httpx.Response) -> bool:
        if method not in _CACHEABLE_METHODS:
            return False
        if upstream.status_code != 200:
            return False
        # Never cache anything that carries a Set-Cookie — it's tied to a
        # specific caller's session, not to the URL.
        if "set-cookie" in {h.lower() for h in upstream.headers}:
            return False
        return len(upstream.content) <= self._settings.cache_max_item_bytes

    @staticmethod
    def _response_from_entry(entry: CacheEntry, *, cache_status: str) -> Response:
        response = Response(
           content=entry.content, status_code=entry.status_code, media_type=entry.media_type
       )
        apply_headers(response, list(entry.headers))
        response.headers["X-Cache"] = cache_status
        return response


    @staticmethod
    def _error_response(status_code: int, message: str) -> Response:
        return Response(
            content=json.dumps({"error": message}),
            status_code=status_code,
            media_type="application/json",
        )