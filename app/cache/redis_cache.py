import asyncio
import base64
import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis

from app.cache import CacheBackend
from app.cache.entry import CacheEntry

logger = logging.getLogger("caching_proxy")

_LOCK_TTL_MS = 10_000  # safety cap: a wedged holder can't block a key forever
_LOCK_POLL_INTERVAL_S = 0.05
_LOCK_MAX_WAIT_S = 5.0

# Released only if the caller still holds the lock it thinks it holds —
# otherwise a slow holder could delete a *different* process's lock after
# its own expired.
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class RedisCache(CacheBackend):
    """A cache shared across every replica of the proxy, backed by Redis.

    This is what makes the proxy horizontally scalable: N processes behind
    a load balancer see one logical cache instead of N independent ones.
    TTL is delegated to Redis's own key expiry (`PX`) rather than tracked
    in application code, and stampede protection uses a short-lived
    `SET NX PX` lock rather than an in-process `asyncio.Lock` (which,
    being per-process memory, can't coordinate across replicas anyway).

    The distributed lock here is intentionally simple, not a full Redlock:
    if it fails to acquire within `_LOCK_MAX_WAIT_S`, callers proceed
    without it. Worst case is an occasional duplicate origin fetch across
    replicas, not a correctness problem — the goal is to *reduce* stampede
    load, not guarantee a single fetch under all failure modes.
    """

    def __init__(
        self,
        *,
        url: str,
        key_prefix: str = "caching-proxy:",
    ) -> None:
        try:
            import redis.asyncio as redis_asyncio
        except ImportError as exc:
            raise RuntimeError(
                "cache_backend='redis' requires the 'redis' package "
                "(it's a normal dependency of this project, so `poetry install` "
                "should have installed it — check your environment)."
            ) from exc

        self._redis = redis_asyncio.from_url(url, decode_responses=False)
        self._prefix = key_prefix
        self._release_script = self._redis.register_script(_RELEASE_SCRIPT)

    def _key(self, key: str) -> str:
        return f"{self._prefix}entry:{key}"

    def _lock_key(self, key: str) -> str:
        return f"{self._prefix}lock:{key}"

    @staticmethod
    def _serialize(entry: CacheEntry) -> bytes:
        payload = {
            "status_code": entry.status_code,
            "headers": list(entry.headers),
            "content_b64": base64.b64encode(entry.content).decode("ascii"),
            "media_type": entry.media_type,
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _deserialize(raw: bytes | str) -> CacheEntry:
        payload = json.loads(raw)
        return CacheEntry(
            status_code=int(payload["status_code"]),
            headers=tuple((h[0], h[1]) for h in payload["headers"]),
            content=base64.b64decode(payload["content_b64"]),
            media_type=payload["media_type"],
        )

    async def get(self, key: str) -> CacheEntry | None:
        raw = await self._redis.get(self._key(key))
        if raw is None:
            return None
        try:
            return self._deserialize(raw)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("dropping corrupt cache entry key=%s: %s", key, exc)
            await self._redis.delete(self._key(key))
            return None

    async def set(self, key: str, entry: CacheEntry, *, ttl_seconds: float) -> None:
        ttl_ms = max(1, int(ttl_seconds * 1000))
        await self._redis.set(self._key(key), self._serialize(entry), px=ttl_ms)

    async def clear(self) -> None:
        """Delete only entry keys belonging to this cache instance.

        Lock keys and any other keys that share the Redis instance are left alone.
        """
        pattern = f"{self._prefix}entry:*"
        # Collect first so we don't mutate the keyspace while iterating
        keys_to_delete = [
            redis_key async for redis_key in self._redis.scan_iter(match=pattern, count=500)
        ]
        if keys_to_delete:
            await self._redis.delete(*keys_to_delete)

    async def size(self) -> int:
        pattern = f"{self._prefix}entry:*"
        count = 0
        async for _ in self._redis.scan_iter(match=pattern, count=500):
            count += 1
        return count

    async def ready(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except redis.ConnectionError:
            return False

    async def aclose(self) -> None:
        await self._redis.aclose()

    @asynccontextmanager
    async def lock_for(self, key: str) -> AsyncIterator[None]:
        lock_key = self._lock_key(key)
        token = secrets.token_hex(16)
        acquired = False
        waited = 0.0

        while waited < _LOCK_MAX_WAIT_S:
            acquired = bool(await self._redis.set(lock_key, token, nx=True, px=_LOCK_TTL_MS))
            if acquired:
                break

            await asyncio.sleep(_LOCK_POLL_INTERVAL_S)
            waited += _LOCK_POLL_INTERVAL_S

        if not acquired:
            logger.info("cache stampede lock busy, proceeding without it key=%s", key)

        try:
            yield
        finally:
            if acquired:
                await self._release_script(keys=[lock_key], args=[token])