"""Application configuration.

Settings are constructed explicitly (via `build_settings`) rather than
mutated in place on a module-level singleton, so the same process can
never have two callers racing to change the origin/port out from under
each other, and tests can construct isolated `Settings` instances.
"""

from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, loadable from environment variables or a
    `.env` file (prefix: `CACHING_PROXY_`), with CLI flags taking
    precedence when provided.
    """

    model_config = SettingsConfigDict(
        env_prefix="CACHING_PROXY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    origin: str = Field(..., description="Origin server to proxy requests to")
    port: int = Field(default=5000, ge=1, le=65535)
    host: str = Field(default="0.0.0.0")

    # Cache behavior
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str | None = Field(default=None, description="e.g. redis://localhost:6379/0 - required if cache_backend=redis")
    cache_ttl_seconds: float = Field(
        default=300.0, gt=0, description="Fallback TTL when the origin gives no Cache-Control max-age"
    )
    cache_max_entries: int = Field(default=1000, gt=0, description="In-memory backend only")
    cache_max_item_bytes: int = Field(
        default=5 * 1024 * 1024,
        gt=0,
        description="Responses larger than this are forwarded but never cached",
    )
    # Headers whose values (when present on the request) become part of the
    # cache key. This is what prevents one caller's response from being
    # served back to a different caller (e.g. two users hitting the same
    # path with different bearer tokens). A response whose Vary header
    # names anything outside this set is treated as uncacheable rather
    # than guessed at — see app/proxy/cache_control.py.
    cache_vary_headers: tuple[str, ...] = (
        "authorization",
        "accept-encoding",
        "accept",
        "origin",
    )

    # Upstream HTTP behavior
    request_timeout_seconds: float = Field(default=10.0, gt=0)

    # Responses larger than this are streamed straight through to the
    # client without ever being buffered in memory (and are never
    # cached, regardless of cache_max_item_bytes) — protects the proxy's
    # own memory from a single huge download, independent of caching.
    max_buffered_response_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    # Request bodies larger than this are rejected with 413 rather than
    # buffered — an unbounded `await request.body()` is a memory-
    # exhaustion vector for any client that can reach this proxy.
    max_request_body_bytes: int = Field(default=10 * 1024 * 1024, gt=0)

    # Admin endpoint (cache clearing) — unauthenticated cache-flush endpoints
    # are a (low-severity but real) griefing vector, so this is opt-in.
    admin_token: str | None = None

    # Rate limiting (opt-in; off by default so existing deployments don't
    # suddenly start getting 429s after an upgrade).
    rate_limit_enabled: bool = False
    rate_limit_requests: int = Field(default=100, gt=0)
    rate_limit_window_seconds: float = Field(default=60.0, gt=0)

    # Set only when the proxy is deployed behind a trusted reverse proxy
    # or load balancer that terminates TLS and sets X-Forwarded-*. Never
    # enable this if the proxy is directly reachable by untrusted clients
    # — otherwise anyone can spoof X-Forwarded-For and evade rate limits,
    # or spoof X-Forwarded-Proto to defeat HSTS-related logic.
    trust_forwarded_headers: bool = False

    metrics_enabled: bool = True

    # TLS termination *inside* the proxy process itself. Optional: the
    # recommended production pattern is TLS termination at a reverse
    # proxy / load balancer in front of this process (see README), but
    # these let uvicorn terminate TLS directly for simpler deployments.
    tls_certfile: str | None = None
    tls_keyfile: str | None = None

    @field_validator("origin")
    @classmethod
    def _validate_origin(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"origin must be an absolute http(s) URL, got: {value!r}"
            )
        if not parsed.netloc:
            raise ValueError(f"origin must include a host, got: {value!r}")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _validate_cross_field_constraints(self) -> "Settings":
        if self.cache_backend == "redis" and not self.redis_url:
            raise ValueError("redis_url is required when cache_backend=redis")
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            raise ValueError("tls_certfile and tls_keyfile must both be set")
        return self


def build_settings(
    *,
    origin: str | None = None,
    port: int | None = None,
    **overrides: Any,
) -> Settings:
    """Build a Settings instance, applying any explicitly-provided
    overrides (e.g. from CLI flags) on top of environment/`.env` values.

    Only keys with a non-None value are applied, so `caching-proxy run`
    without flags still works from env vars.
    """
    kwargs: dict[str, Any] = {k: v for k, v in overrides.items() if v is not None}
    if origin is not None:
        kwargs["origin"] = origin
    if port is not None:
        kwargs["port"] = port
    return Settings(**kwargs)
