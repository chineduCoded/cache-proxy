import asyncio

import pytest

from app.cache.entry import CacheEntry
from app.cache.memory import InMemoryCache


def make_entry(content: bytes = b"x") -> CacheEntry:
    return CacheEntry(status_code=200, headers=(), content=content, media_type="application/json")


@pytest.mark.anyio
async def test_set_then_get_returns_entry():
    cache = InMemoryCache(max_entries=10, default_ttl_seconds=60)
    entry = make_entry()
    await cache.set("k", entry, ttl_seconds=60)
    assert await cache.get("k") is entry


@pytest.mark.anyio
async def test_expired_entry_is_evicted_on_get():
    cache = InMemoryCache(max_entries=10, default_ttl_seconds=60)
    await cache.set("k", make_entry(), ttl_seconds=0.01)
    await asyncio.sleep(0.05)
    assert await cache.get("k") is None
    assert await cache.size() == 0


@pytest.mark.anyio
async def test_lru_eviction_when_over_capacity():
    cache = InMemoryCache(max_entries=2, default_ttl_seconds=60)
    await cache.set("a", make_entry(), ttl_seconds=60)
    await cache.set("b", make_entry(), ttl_seconds=60)
    await cache.set("c", make_entry(), ttl_seconds=60)  # evicts "a" (least recently used)

    assert await cache.get("a") is None
    assert await cache.get("b") is not None
    assert await cache.get("c") is not None
    assert await cache.size() == 2


@pytest.mark.anyio
async def test_get_refreshes_lru_order():
    cache = InMemoryCache(max_entries=2, default_ttl_seconds=60)
    await cache.set("a", make_entry(), ttl_seconds=60)
    await cache.set("b", make_entry(), ttl_seconds=60)
    await cache.get("a")  # "a" is now most-recently-used
    await cache.set("c", make_entry(), ttl_seconds=60)  # should evict "b", not "a"

    assert await cache.get("a") is not None
    assert await cache.get("b") is None


@pytest.mark.anyio
async def test_clear_removes_everything():
    cache = InMemoryCache(max_entries=10, default_ttl_seconds=60)
    await cache.set("a", make_entry(), ttl_seconds=60)
    await cache.clear()
    assert await cache.size() == 0


@pytest.mark.anyio
async def test_zero_or_negative_ttl_falls_back_to_default():
    cache = InMemoryCache(max_entries=10, default_ttl_seconds=60)
    await cache.set("a", make_entry(), ttl_seconds=0)
    assert await cache.get("a") is not None  # didn't instantly expire


@pytest.mark.anyio
async def test_lock_for_serializes_concurrent_misses():
    cache = InMemoryCache(max_entries=10, default_ttl_seconds=60)
    order: list[str] = []

    async def worker(name: str) -> None:
        async with cache.lock_for("shared-key"):
            order.append(f"{name}-start")
            await asyncio.sleep(0.01)
            order.append(f"{name}-end")

    await asyncio.gather(worker("first"), worker("second"))

    assert order in (
        ["first-start", "first-end", "second-start", "second-end"],
        ["second-start", "second-end", "first-start", "first-end"],
    )


@pytest.mark.anyio
async def test_ready_is_always_true_for_memory_backend():
    cache = InMemoryCache(max_entries=10, default_ttl_seconds=60)
    assert await cache.ready() is True
