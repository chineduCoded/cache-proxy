import asyncio

import pytest

from app.cache.entry import CacheEntry
from app.cache.ttl_lru import AsyncTTLLRUCache


def make_entry(expires_at: float, content: bytes = b"x") -> CacheEntry:
    return CacheEntry(
        status_code=200,
        headers=(),
        content=content,
        media_type="application/json",
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_set_then_get_returns_entry():
    cache = AsyncTTLLRUCache(max_entries=10, default_ttl_seconds=60)
    entry = make_entry(expires_at=asyncio.get_event_loop().time() + 60)
    await cache.set("k", entry)
    assert await cache.get("k") is entry


@pytest.mark.asyncio
async def test_expired_entry_is_evicted_on_get():
    cache = AsyncTTLLRUCache(max_entries=10, default_ttl_seconds=60)
    import time

    entry = make_entry(expires_at=time.monotonic() - 1)  # already expired
    await cache.set("k", entry)
    assert await cache.get("k") is None
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_lru_eviction_when_over_capacity():
    cache = AsyncTTLLRUCache(max_entries=2, default_ttl_seconds=60)
    import time

    future = time.monotonic() + 60
    await cache.set("a", make_entry(future))
    await cache.set("b", make_entry(future))
    await cache.set("c", make_entry(future))  # evicts "a" (least recently used)

    assert await cache.get("a") is None
    assert await cache.get("b") is not None
    assert await cache.get("c") is not None
    assert len(cache) == 2


@pytest.mark.asyncio
async def test_get_refreshes_lru_order():
    cache = AsyncTTLLRUCache(max_entries=2, default_ttl_seconds=60)
    import time

    future = time.monotonic() + 60
    await cache.set("a", make_entry(future))
    await cache.set("b", make_entry(future))
    await cache.get("a")  # "a" is now most-recently-used
    await cache.set("c", make_entry(future))  # should evict "b", not "a"

    assert await cache.get("a") is not None
    assert await cache.get("b") is None


@pytest.mark.asyncio
async def test_clear_removes_everything():
    cache = AsyncTTLLRUCache(max_entries=10, default_ttl_seconds=60)
    import time

    await cache.set("a", make_entry(time.monotonic() + 60))
    await cache.clear()
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_lock_for_serializes_concurrent_misses():
    cache = AsyncTTLLRUCache(max_entries=10, default_ttl_seconds=60)
    order: list[str] = []

    async def worker(name: str) -> None:
        async with cache.lock_for("shared-key"):
            order.append(f"{name}-start")
            await asyncio.sleep(0.01)
            order.append(f"{name}-end")

    await asyncio.gather(worker("first"), worker("second"))

    # Whichever runs first must fully finish before the other starts —
    # the two "start"/"end" pairs must not interleave.
    assert order in (
        ["first-start", "first-end", "second-start", "second-end"],
        ["second-start", "second-end", "first-start", "first-end"],
    )
