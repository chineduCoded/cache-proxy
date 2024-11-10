from fastapi import FastAPI, Request, Response,status
from typing import Dict
import typer
import httpx
from typing_extensions import Annotated
import json

from app.utils.settings import get_settings
from app.utils.hashing import get_cached_key

app = FastAPI()
cache: Dict[str, bytes] = {}

cli = typer.Typer()
settings = get_settings()

@app.middleware("http")
async def cache_middleware(request: Request, call_next):
    """Cache middleware to check if the request is already cached"""

    # Only cache GET requests
    if request.method != "GET":
        return await call_next(request)

    # Construct original URL
    original_url = f"{settings.origin.rstrip("/")}{request.url.path}"
    if request.query_params:
        original_url = f"{original_url}?{str(request.query_params)}"
    cache_key = get_cached_key(original_url)

    if cache_key in cache:
        return Response(
            content=cache[cache_key], 
            headers={
                "X-Cache": "HIT", 
                "Content-Type": "application/json"
            }
        )

    async with httpx.AsyncClient() as client:
        try:
            original_response = await client.get(
                original_url,
                headers={k: v for k, v in request.headers.items() if k.lower() not in ['host']}
            )
            
            cache[cache_key] = original_response.content
        except httpx.InvalidURL as exc:
            return Response(
                content=f"Invalid URL: {exc}",
                status_code=status.HTTP_400_BAD_REQUEST,
                media_type="application/json"
            )
        except httpx.RequestError as exc:
            return Response(
                content=f"Error fetching from origin: {exc}",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                media_type="application/json"
            )

    return Response(
        content=original_response.content,
        status_code=original_response.status_code,
        headers={
            **dict(original_response.headers),
            "X-Cache": "MISS",
            "Content-Type": "application/json"
            }
        )

@cli.command("caching-proxy")
def caching_proxy(
    port: Annotated[int, typer.Option("--port", "-p", help="Port to run the server")] = None,
    origin: Annotated[str, typer.Option("--origin", "-o", help="Origin server")] = "",
    clear_cache: Annotated[bool, typer.Option("--clear-cache", "-c", help="Clear cache")] = False
):
    """
    Start Caching proxy server
    """
    global settings
    settings.port = port
    settings.origin = origin

    if clear_cache:
        cache.clear()
        typer.echo("Cache cleared.")
        return

    import uvicorn
    try:
        uvicorn.run(app, host="0.0.0.0", port=settings.port)
    except Exception as e:
        typer.echo(f"Failed to start server: {e}")
        raise typer.Exit(code=1)

if __name__ == "__main__":
    cli()