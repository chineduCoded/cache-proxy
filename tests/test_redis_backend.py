"""Tests for RedisCache against a minimal fake standing in for
redis.asyncio.Redis — enough surface area to exercise our own logic
(serialization, TTL, SCAN-based clear, the release-if-owner lock script)
without requiring a real Redis server in CI.
"""
import time

import pytest

from app.cache.entry import CacheEntry


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, tuple[bytes, float | None]] = {}
        self.scripts: list[str] = []


    def register_script(self, script: str):
        self.scripts.append(script)

        async def run(keys, args):
            key = keys[0]
            token = args[0]
            current = await self.get(key)
            if current is not None and current == token:
                return await self.delete(key)
            return 0

        return run

    async def get(self, key: str):
        item = self._store.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at is not None and time.monotonic() >= expires_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: bytes, px: int | None = None, nx: bool = False):
        if nx and key in self._store:
            return False
        expires_at = time.monotonic() + px / 1000 if px is not None else None
        self._store[key] = (value, expires_at)
        return True

    async def delete(self, *keys: str):
        count = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                count += 1
        return count

    async def scan_iter(self, match: str, count: int = 500):
        prefix = match.rstrip("*")
        for key in list(self._store.keys()):
            if key.startswith(prefix):
                yield key

    async def ping(self):
        return True

    async def aclose(self):
        pass


@pytest.fixture
def redis_cache():
    from app.cache import redis_cache

    fake = _FakeRedis()
    cache = redis_cache.RedisCache.__new__(redis_cache.RedisCache)
    cache._redis = fake
    cache._prefix = "test:"
    cache._release_script = fake.register_script(redis_cache._RELEASE_SCRIPT)
    return cache


def make_entry(content: bytes = b"hello") -> CacheEntry:
    return CacheEntry(
        status_code=200,
        headers=(("content-type", "application/json"),),
        content=content,
        media_type="application/json",
    )


@pytest.mark.anyio
async def test_set_then_get_roundtrips(redis_cache):
    entry = make_entry()
    await redis_cache.set("k", entry, ttl_seconds=60)
    result = await redis_cache.get("k")
    assert result == entry


@pytest.mark.anyio
async def test_get_missing_key_returns_none(redis_cache):
    assert await redis_cache.get("nope") is None


@pytest.mark.anyio
async def test_clear_removes_only_entry_keys(redis_cache):
    await redis_cache.set("a", make_entry(), ttl_seconds=60)
    await redis_cache.set("b", make_entry(), ttl_seconds=60)
    await redis_cache.clear()
    assert await redis_cache.get("a") is None
    assert await redis_cache.get("b") is None


@pytest.mark.anyio
async def test_size_counts_entries(redis_cache):
    await redis_cache.set("a", make_entry(), ttl_seconds=60)
    await redis_cache.set("b", make_entry(), ttl_seconds=60)
    assert await redis_cache.size() == 2


@pytest.mark.anyio
async def test_ready_pings_redis(redis_cache):
    assert await redis_cache.ready() is True


@pytest.mark.anyio
async def test_lock_for_acquires_and_releases(redis_cache):
    async with redis_cache.lock_for("key"):
        pass
    # Lock key should be released (deleted) afterwards, so a second
    # acquisition doesn't have to wait out the lock TTL.
    assert await redis_cache._redis.get(redis_cache._lock_key("key")) is None


@pytest.mark.anyio
async def test_corrupt_entry_is_dropped_not_raised(redis_cache):
    await redis_cache._redis.set(redis_cache._key("bad"), b"not json", px=60_000)
    assert await redis_cache.get("bad") is None
