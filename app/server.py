import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse

from app.cache import build_cache
from app.config import Settings
from app.metrics import Metrics
from app.proxy.service import ProxyService
from app.security import (
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    builder_rate_limiter,
    constant_time_equals,
)

logger = logging.getLogger("caching_proxy")


def create_app(settings: Settings) -> FastAPI:
    metrics = Metrics() if settings.metrics_enabled else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # One shared client for the process lifetime: reuses connections
        # instead of paying TCP/TLS setup cost on every proxied request.
        async with httpx.AsyncClient() as client:
            cache = build_cache(settings)
            app.state.cache = cache
            app.state.proxy_service = ProxyService(
                settings=settings, client=client, cache=cache, metrics=metrics
            )
            logger.info(
                "caching-proxy started origin=%s port=%s backend=%s ttl=%ss "
                "max_entries=%s rate_limit=%s",
                settings.origin,
                settings.port,
                settings.cache_backend,
                settings.cache_ttl_seconds,
                settings.cache_max_entries,
                settings.rate_limit_enabled,
            )
            try:
                yield
            finally:
                logger.info("caching-proxy shutting down")

    app = FastAPI(lifespan=lifespan)

    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts=bool(settings.tls_certfile),
    )

    limiter = builder_rate_limiter(settings)
    if limiter is not None:
        app.add_middleware(
            RateLimitMiddleware,
            limiter=limiter,
        )

    @app.get("/_health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "cache_entries": await app.state.cache.size()
        }

    @app.get("/_ready")
    async def ready(response: Response) -> dict[str, Any]:
        """Readiness: can this instance actually serve traffic right now.
        Checked separately from liveness so an orchestrator can stop
        routing new traffic here without killing the process.
        """
        cache_ready = await app.state.cache.ready()
        if not cache_ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if cache_ready else "unavailable",
            "cache_backend": settings.cache_backend,
            "cache_entries": await app.state.cache.size(),
        }

    if metrics is not None:
        @app.get("/metrics")
        async def render_metrics() -> Response:
            from app.metrics import CONTENT_TYPE_LATEST

            return PlainTextResponse(metrics.render(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/_internal/cache/clear")
    async def clear_cache(x_admin_token: str | None = Header(default=None)) -> dict[str, str]:
        if not settings.admin_token:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="admin endpoint is disabled"
            )
        if not x_admin_token or not constant_time_equals(x_admin_token, settings.admin_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid admin token")
        await app.state.cache.clear()
        logger.info("cache cleared via admin endpoint")
        return {"status": "cleared"}

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def proxy_all(path: str, request: Request) -> Response:
        service: ProxyService = app.state.proxy_service
        return await service.handle(request)

    return app