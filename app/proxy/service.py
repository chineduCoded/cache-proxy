import json
import logging
import time

import httpx
from fastapi import Request, Response

from app.cache.entry import CacheEntry
from app.cache.ttl_lru import AsyncTTLLRUCache
from app.config import Settings
from app.proxy.headers import filter_request_headers, filter_response_headers
from app.proxy.keys import build_cache_key

logger = logging.getLogger("caching_proxy")

_CACHEABLE_METHODS = {"GET"}


class ProxyService:
    """Forwards requests to the configured origin, transparently caching
    cacheable responses.

    Holds its dependencies (HTTP client, cache, settings) explicitly rather
    than reaching for module globals, so it can be constructed once per app
    (real deployment) or once per test (isolated, no shared state).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        client: httpx.AsyncClient,
        cache: AsyncTTLLRUCache,
    ) -> None:
        self._settings = settings
        self._client = client
        self._cache = cache

    def _target_url(self, path: str, query_string: str) -> str:
        url = f"{self._settings.origin}/{path.lstrip('/')}"
        if query_string:
            url = f"{url}?{query_string}"
        return url

    async def handle(self, request: Request) -> Response:
        method = request.method.upper()
        target_url = self._target_url(request.url.path, request.url.query)

        if method not in _CACHEABLE_METHODS:
            response, _ = await self._fetch(request, target_url, method, cache=False)
            return response

        request_headers = dict(request.headers)
        cache_key = build_cache_key(
            method=method,
            url=target_url,
            request_headers=request_headers,
            vary_headers=self._settings.cache_vary_headers,
        )

        cached = await self._cache.get(cache_key)
        if cached is not None:
            return self._response_from_entry(cached, cache_status="HIT")

        # Stampede protection: only one concurrent request per cache key
        # actually reaches the origin; the rest wait and then re-check.
        async with self._cache.lock_for(cache_key):
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return self._response_from_entry(cached, cache_status="HIT")

            response, entry = await self._fetch(request, target_url, method, cache=True)
            if entry is not None:
                await self._cache.set(cache_key, entry)
            response.headers["X-Cache"] = "MISS"
            return response

    async def _fetch(
        self,
        request: Request,
        target_url: str,
        method: str,
        *,
        cache: bool,
    ) -> tuple[Response, CacheEntry | None]:
        body = await request.body()
        request_headers = filter_request_headers(dict(request.headers))

        try:
            upstream = await self._client.request(
                method,
                target_url,
                headers=request_headers,
                content=body or None,
                timeout=self._settings.request_timeout_seconds,
            )
        except httpx.InvalidURL as exc:
            return self._error_response(400, f"Invalid URL: {exc}"), None
        except httpx.TimeoutException as exc:
            logger.warning("upstream timeout method=%s url=%s: %s", method, target_url, exc)
            return self._error_response(504, f"Upstream timed out: {exc}"), None
        except httpx.RequestError as exc:
            logger.warning("upstream error method=%s url=%s: %s", method, target_url, exc)
            return self._error_response(502, f"Error fetching from origin: {exc}"), None

        response_headers = filter_response_headers(dict(upstream.headers))
        media_type = upstream.headers.get("content-type")
        response = Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=media_type,
        )

        entry = None
        if cache and self._is_cacheable(method, upstream):
            entry = CacheEntry(
                status_code=upstream.status_code,
                headers=tuple(response_headers.items()),
                content=upstream.content,
                media_type=media_type,
                expires_at=time.monotonic() + self._cache.ttl_for(),
            )
        return response, entry

    def _is_cacheable(self, method: str, upstream: httpx.Response) -> bool:
        if method not in _CACHEABLE_METHODS:
            return False
        if upstream.status_code != 200:
            return False
        # Never cache anything that carries a Set-Cookie — it's tied to a
        # specific caller's session, not to the URL.
        if "set-cookie" in {h.lower() for h in upstream.headers}:
            return False
        return len(upstream.content) <= self._settings.cache_max_item_bytes

    @staticmethod
    def _response_from_entry(entry: CacheEntry, *, cache_status: str) -> Response:
        headers = dict(entry.headers)
        headers["X-Cache"] = cache_status
        return Response(
            content=entry.content,
            status_code=entry.status_code,
            headers=headers,
            media_type=entry.media_type,
        )

    @staticmethod
    def _error_response(status_code: int, message: str) -> Response:
        return Response(
            content=json.dumps({"error": message}),
            status_code=status_code,
            media_type="application/json",
        )