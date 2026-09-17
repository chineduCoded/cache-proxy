import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, status

from app.cache.ttl_lru import AsyncTTLLRUCache
from app.config import Settings
from app.proxy.service import ProxyService

logger = logging.getLogger("caching_proxy")


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # One shared client for the process lifetime: reuses connections
        # instead of paying TCP/TLS setup cost on every proxied request.
        async with httpx.AsyncClient() as client:
            cache = AsyncTTLLRUCache(
                max_entries=settings.cache_max_entries,
                default_ttl_seconds=settings.cache_ttl_seconds,
            )
            app.state.cache = cache
            app.state.proxy_service = ProxyService(
                settings=settings, client=client, cache=cache
            )
            logger.info(
                "caching-proxy started origin=%s port=%s ttl=%ss max_entries=%s",
                settings.origin,
                settings.port,
                settings.cache_ttl_seconds,
                settings.cache_max_entries,
            )
            yield
            logger.info("caching-proxy shutting down")

    app = FastAPI(lifespan=lifespan)

    @app.get("/_health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "cache_entries": len(app.state.cache)}

    @app.post("/_internal/cache/clear")
    async def clear_cache(x_admin_token: str | None = Header(default=None)) -> dict[str, str]:
        if not settings.admin_token:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="admin endpoint is disabled"
            )
        if x_admin_token != settings.admin_token:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid admin token")
        await app.state.cache.clear()
        logger.info("cache cleared via admin endpoint")
        return {"status": "cleared"}

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def proxy_all(path: str, request: Request) -> Response:
        service: ProxyService = app.state.cache.service
        return await service.handle(request)

    return app