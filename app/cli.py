from typing import Annotated

import httpx
import typer
import uvicorn

from app.config import build_settings
from app.logging_config import configure_logging
from app.server import create_app

cli = typer.Typer(
    help="Caching proxy: forwards requests to an origin server and caches GET responses."
)


@cli.command("run")
def run(
    origin: Annotated[
        str | None,
        typer.Option(
            "--origin",
            "-o",
            help="Origin server, e.g. http://dummyjson.com "
            "(or set CACHING_PROXY_ORIGIN)",
        ),
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", "-p", help="Port to run the server on")
    ] = None,
    cache_backend: Annotated[
        str | None,
        typer.Option(
            "--cache-backend",
            help="'memory' (default, single-process) or 'redis' (shared across replicas)",
        ),
    ] = None,
    redis_url: Annotated[
        str | None,
        typer.Option("--redis-url", help="e.g. redis://localhost:6379/0 — required with --cache-backend redis"),
    ] = None,
    cache_ttl: Annotated[
        float | None, typer.Option("--cache-ttl", help="Cache entry lifetime in seconds")
    ] = None,
    cache_max_entries: Annotated[
        int | None, typer.Option("--cache-max-entries", help="Max cached responses")
    ] = None,
    admin_token: Annotated[
        str | None,
        typer.Option(
            "--admin-token",
            help="Enables POST /_internal/cache/clear when set (required header: X-Admin-Token)",
        ),
    ] = None,
    rate_limit: Annotated[
        bool | None,
        typer.Option("--rate-limit/--no-rate-limit", help="Enable per-IP rate limiting"),
    ] = None,
    rate_limit_requests: Annotated[
        int | None, typer.Option("--rate-limit-requests", help="Requests allowed per window")
    ] = None,
    rate_limit_window: Annotated[
        float | None, typer.Option("--rate-limit-window", help="Rate limit window, in seconds")
    ] = None,
    trust_forwarded_headers: Annotated[
        bool | None,
        typer.Option(
            "--trust-forwarded-headers/--no-trust-forwarded-headers",
            help="Trust X-Forwarded-* from the immediate peer — only enable behind a "
            "TLS-terminating reverse proxy / load balancer you control",
        ),
    ] = None,
    tls_certfile: Annotated[
        str | None, typer.Option("--tls-certfile", help="Terminate TLS in-process with this cert")
    ] = None,
    tls_keyfile: Annotated[
        str | None, typer.Option("--tls-keyfile", help="Terminate TLS in-process with this key")
    ] = None,
    graceful_shutdown_timeout: Annotated[
        float, typer.Option("--graceful-shutdown-timeout", help="Seconds to drain in-flight requests on shutdown")
    ] = 10.0,
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
) -> None:
    """Start the caching proxy server."""
    configure_logging(log_level)

    try:
        settings = build_settings(
            origin=origin,
            port=port,
            cache_backend=cache_backend,
            redis_url=redis_url,
            cache_ttl_seconds=cache_ttl,
            cache_max_entries=cache_max_entries,
            admin_token=admin_token,
            rate_limit_enabled=rate_limit,
            rate_limit_requests=rate_limit_requests,
            rate_limit_window_seconds=rate_limit_window,
            trust_forwarded_headers=trust_forwarded_headers,
            tls_certfile=tls_certfile,
            tls_keyfile=tls_keyfile,
        )
    except ValueError as exc:
        typer.echo(f"Invalid configuration: {exc}", err=True)
        raise typer.Exit(code=1)

    app = create_app(settings)
    try:
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            proxy_headers=settings.trust_forwarded_headers,
            forwarded_allow_ips="*" if settings.trust_forwarded_headers else None,
            ssl_certfile=settings.tls_certfile,
            ssl_keyfile=settings.tls_keyfile,
            timeout_graceful_shutdown=int(graceful_shutdown_timeout),
        )
    except ConnectionError as exc:
        typer.echo(f"Failed to start server: {exc}", err=True)
        raise typer.Exit(code=1)


@cli.command("clear-cache")
def clear_cache(
    host: Annotated[str, typer.Option("--host", help="Host of a running proxy instance")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port of a running proxy instance")] = 5000,
    admin_token: Annotated[
        str, typer.Option("--admin-token", help="Must match the running instance's --admin-token")
    ] = "",
    scheme: Annotated[str, typer.Option("--scheme", help="'http' or 'https'")] = "http",
) -> None:
    """Clear the cache of a *running* proxy instance.

    The cache lives in the server process's memory, so this talks to that
    process over HTTP rather than touching any local state — there's
    nothing else it could mean.
    """
    url = f"{scheme}://{host}:{port}/_internal/cache/clear"
    try:
        response = httpx.post(url, headers={"X-Admin-Token": admin_token}, timeout=5.0)
    except httpx.RequestError as exc:
        typer.echo(f"Could not reach proxy at {url}: {exc}", err=True)
        raise typer.Exit(code=1)

    if response.status_code == 200:
        typer.echo("Cache cleared.")
    elif response.status_code == 404:
        typer.echo(
            "Admin endpoint is disabled on that instance (start it with --admin-token).",
            err=True,
        )
        raise typer.Exit(code=1)
    else:
        typer.echo(f"Failed to clear cache: {response.status_code} {response.text}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    cli()