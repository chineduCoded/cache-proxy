from contextlib import AbstractAsyncContextManager
from typing import Protocol

from app.cache.entry import CacheEntry


class CacheBackend(Protocol):
    """Shared interface for in-process and external (e.g. Redis) caches."""

    async def get(self, key: str) -> CacheEntry | None: ...

    async def set(self, key: str, entry: CacheEntry, *, ttl_seconds: float) -> None: ...

    async def clear(self) -> None: ...

    async def size(self) -> int: ...

    async def ready(self) -> bool:
        """
        Used by the /_ready endpoint: can this backend actually be
        reached right now (e.g. is Redis up)?
        """
        ...

    def lock_for(self, key: str) -> AbstractAsyncContextManager[None]:
        """
        Serializes concurrent cache-miss handling for one key, so N
        simultaneous requests for the same cold URL don't all hit the
        origin at once (a cache stampede).
        """
        ...