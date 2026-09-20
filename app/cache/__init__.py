from app.cache.backend import CacheBackend
from app.cache.entry import CacheEntry
from app.cache.memory import InMemoryCache

__all__ = ["CacheBackend", "CacheEntry", "InMemoryCache", "build_cache"]

from app.config import Settings


def build_cache(
       settings: Settings,
) -> CacheBackend:
    """Return an in-process or Redis-backed cache depending on configuration."""
    if settings.cache_backend == "memory":
        return InMemoryCache(
            max_entries=settings.cache_max_entries,
            default_ttl_seconds=settings.cache_ttl_seconds,
        )

    if settings.cache_backend == "redis":
        if not settings.redis_url:
            raise ValueError("cache_backend='redis' requires 'redis_url' to be set")
        from app.cache.redis_cache import RedisCache

        return RedisCache(url=settings.redis_url)
    raise ValueError(f"unknown cache_backend: {settings.cache_backend!r}")