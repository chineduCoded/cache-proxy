# Caching Proxy

A CLI-driven caching reverse proxy: forwards requests to an origin server
and transparently caches `GET` responses.

## Install

```bash
pipx install poetry   # if you don't have it
poetry install
```

## Run

```bash
poetry run caching-proxy run --origin https://dummyjson.com --port 5000
```

In another terminal:

```bash
curl -i http://127.0.0.1:5000/products     # X-Cache: MISS
curl -i http://127.0.0.1:5000/products     # X-Cache: HIT
```

Non-`GET` requests (`POST`/`PUT`/`PATCH`/`DELETE`) are forwarded transparently
and are never cached.

### Clearing the cache

The cache lives in the running server process's memory, so clearing it means
talking to that process, not touching local files. Start the server with an
admin token to enable this:

```bash
caching-proxy run --origin https://dummyjson.com --admin-token secret
caching-proxy clear-cache --port 5000 --admin-token secret
```

Without `--admin-token`, the clear-cache endpoint is disabled (404) — an
unauthenticated cache-flush endpoint is an easy griefing vector, so it's
opt-in.

### Configuration

Every `run` flag can also be set via environment variable (prefix
`CACHING_PROXY_`) or a `.env` file, e.g. `CACHING_PROXY_ORIGIN`,
`CACHING_PROXY_CACHE_TTL_SECONDS`, `CACHING_PROXY_ADMIN_TOKEN`. CLI flags
take precedence when both are set.

| Setting | Flag | Default |
|---|---|---|
| Origin (required) | `--origin` / `-o` | — |
| Port | `--port` / `-p` | `5000` |
| Cache TTL | `--cache-ttl` | `300` seconds |
| Max cached entries | `--cache-max-entries` | `1000` |
| Admin token | `--admin-token` | disabled |

Responses larger than `cache_max_item_bytes` (default 5 MB), non-`200`
responses, and responses carrying `Set-Cookie` are forwarded but never
cached. The cache key includes the value of any header listed in
`cache_vary_headers` (default: `Authorization`), so two callers with
different credentials never share a cached response for the same path.

## Operational endpoints

- `GET /_health` — liveness + current cache size.
- `POST /_internal/cache/clear` — see above; requires `X-Admin-Token`.

## Development

```bash
poetry install --with dev
poetry run pytest
poetry run ruff check .
poetry run mypy app
```

## Architecture

```
app/
  cli.py            Typer CLI: `run`, `clear-cache`
  config.py         pydantic-settings Settings, validated at startup
  server.py         FastAPI app factory (lifespan-managed HTTP client, routes)
  logging_config.py Structured (JSON) logging setup
  cache/
    entry.py        Immutable CacheEntry
    ttl_lru.py       Size- and TTL-bounded async cache with stampede locks
  proxy/
    service.py       Core forward/cache logic
    headers.py        Hop-by-hop header filtering
    keys.py            Cache-key construction (vary-header aware)
```

No module-level mutable globals: settings, the cache, and the shared
`httpx.AsyncClient` are constructed once in `create_app`'s lifespan and
passed explicitly into `ProxyService`, which makes the whole thing
straightforward to unit-test in isolation (see `tests/`).
