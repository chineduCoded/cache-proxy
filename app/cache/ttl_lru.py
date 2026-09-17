import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.cache.entry import CacheEntry


class AsyncTTLLRUCache:
    """An in-memory cache bounded by both entry count (LRU eviction) and
    per-entry TTL.

    Two things the original module-level ``dict`` cache didn't have, and
    that any production cache needs:

    1. **A size bound.** Without one, an attacker (or just organic traffic
       against a large origin) can grow the cache without limit until the
       process is OOM-killed.
    2. **Per-key locking for stampede protection.** Without it, N
       concurrent requests for the same cold URL all miss the cache and
       all hit the origin at once.
    """

    def __init__(self, *, max_entries: int, default_ttl_seconds: float) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive")
        self._max_entries = max_entries
        self._default_ttl = default_ttl_seconds
        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        self._data_lock = asyncio.Lock()
        # Per-key locks so concurrent misses for the *same* key serialize,
        # while misses for *different* keys don't block each other.
        self._key_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._key_locks_guard = asyncio.Lock()

    async def get(self, key: str) -> CacheEntry | None:
        now = time.monotonic()
        async with self._data_lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.is_expired(now):
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return entry

    async def set(self, key: str, entry: CacheEntry) -> None:
        async with self._data_lock:
            self._data[key] = entry
            self._data.move_to_end(key)
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)  # evict least-recently-used

    async def clear(self) -> None:
        async with self._data_lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

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

    def ttl_for(self, override_seconds: float | None = None) -> float:
        return override_seconds if override_seconds is not None else self._default_ttl