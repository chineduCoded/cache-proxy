"""Application configuration.

Settings are constructed explicitly (via `build_settings`) rather than
mutated in place on a module-level singleton, so the same process can
never have two callers racing to change the origin/port out from under
each other, and tests can construct isolated `Settings` instances.
"""

from typing import Any
from urllib.parse import urlparse

from pydantic import Field, field_validator
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
    cache_ttl_seconds: float = Field(default=300.0, gt=0)
    cache_max_entries: int = Field(default=1000, gt=0)
    cache_max_item_bytes: int = Field(
        default=5 * 1024 * 1024,
        gt=0,
        description="Responses larger than this are forwarded but never cached",
    )
    # Headers whose values (when present on the request) become part of the
    # cache key. This is what prevents one caller's response from being
    # served back to a different caller (e.g. two users hitting the same
    # path with different bearer tokens).
    cache_vary_headers: tuple[str, ...] = ("authorization",)

    # Upstream HTTP behavior
    request_timeout_seconds: float = Field(default=10.0, gt=0)

    # Admin endpoint (cache clearing) — unauthenticated cache-flush endpoints
    # are a (low-severity but real) griefing vector, so this is opt-in.
    admin_token: str | None = None

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
