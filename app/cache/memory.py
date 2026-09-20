import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.cache.backend import CacheBackend
from app.cache.entry import CacheEntry


class InMemoryCache(CacheBackend):
    """An in-process cache bounded by both entry count (LRU eviction) and
    per-entry TTL.

    This is the zero-setup default. It has one hard limitation that's
    inherent to the approach, not a bug: it is **per-process**. Running
    more than one replica of the proxy behind a load balancer gives each
    replica its own independent, inconsistent cache — a request that's a
    HIT on replica A may be a MISS on replica B. For a single instance (or
    for horizontal scaling where that's acceptable) this is the simplest
    correct choice; for anything else, use `RedisCache` instead.
    """

    def __init__(self, *, max_entries: int, default_ttl_seconds: float) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive")
        self._max_entries = max_entries
        self._default_ttl = default_ttl_seconds
        self._data: OrderedDict[str, tuple[CacheEntry, float]] = OrderedDict()
        self._data_lock = asyncio.Lock()
        # Per-key locks so concurrent misses for the *same* key serialize,
        # while misses for *different* keys don't block each other.
        self._key_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._key_locks_guard = asyncio.Lock()

    async def get(self, key: str) -> CacheEntry | None:
        now = time.monotonic()
        async with self._data_lock:
            item = self._data.get(key)
            if item is None:
                return None
            entry, expires_at = item
            if now >= expires_at:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return entry

    async def set(self, key: str, entry: CacheEntry, *, ttl_seconds: float) -> None:
        ttl = ttl_seconds if ttl_seconds > 0 else self._default_ttl
        async with self._data_lock:
            self._data[key] = (entry, time.monotonic() + ttl)
            self._data.move_to_end(key)
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)  # evict least-recently-used

    async def clear(self) -> None:
        async with self._data_lock:
            self._data.clear()

    async def size(self) -> int:
        return len(self._data)

    async def ready(self) -> bool:
        return True  # nothing external to be unready

    @asynccontextmanager
    async def lock_for(self, key: str) -> AsyncIterator[None]:
        """Serialize concurrent cache-miss handling for a single key."""
        async with self._key_locks_guard:
            lock, refcount = self._key_locks.get(key, (asyncio.Lock(), 0))
            self._key_locks[key] = (lock, refcount + 1)

        try:
            async with lock:
                yield
        finally:
            async with self._key_locks_guard:
                existing_lock, refcount = self._key_locks[key]
                if refcount <= 1:
                    del self._key_locks[key]
                else:
                    self._key_locks[key] = (existing_lock, refcount - 1)