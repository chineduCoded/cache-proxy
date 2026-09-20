# Caching Proxy

A CLI-driven caching reverse proxy for HTTP services.

The proxy forwards requests to an origin server and transparently caches eligible `GET` responses according to the origin's `Cache-Control` and `Vary` headers.

It supports both an in-process memory cache for single-instance deployments and Redis for horizontally scaled deployments.

---

## Features

* **HTTP-aware caching** based on origin `Cache-Control` and `Vary` headers
* **Memory cache** with TTL and entry-count limits
* **Redis cache** for shared caching across replicas
* **Cache-stampede protection**
* **Vary-aware cache keys**
* **Duplicate-header-safe response forwarding**
* **Request and response size limits**
* **Optional rate limiting**
* **TLS termination**
* **Forwarded-header support**
* **Prometheus metrics**
* **Health and readiness endpoints**
* **Admin-protected cache invalidation**
* **Structured logging**
* **CLI and environment-based configuration**
* **Graceful shutdown**

---

## How It Works

At a high level:

```text
                         ┌─────────────────┐
                         │   Origin Server │
                         └────────┬────────┘
                                  │
                                  │ HTTP
                                  │
┌──────────┐       ┌──────────────▼──────────────┐
│  Client  │ ────▶ │       Caching Proxy         │
└──────────┘       │                              │
                   │  Cache lookup                │
                   │  Cache policy                │
                   │  Request forwarding          │
                   │  Response handling           │
                   └──────────────┬──────────────┘
                                  │
                         ┌────────▼────────┐
                         │ Memory / Redis  │
                         │     Cache       │
                         └─────────────────┘
```

Only eligible `GET` responses are cached.

Non-`GET` requests such as `POST`, `PUT`, `PATCH`, and `DELETE` are forwarded transparently and are never cached.

---

## Requirements

* Python 3.13
* Poetry
* Redis, if using the Redis cache backend

---

## Installation

Install the project dependencies with Poetry:

```bash
pipx install poetry
poetry install
```

For development dependencies:

```bash
poetry install --with dev
```

---

## Quick Start

Start the proxy with an origin server:

```bash
poetry run caching-proxy run \
  --origin https://api.open-meteo.com
```

The proxy listens on port `5000` by default.

Test it with:

```bash
curl -i \
  "http://127.0.0.1:5000/v1/forecast?latitude=52.52&longitude=13.41&current_weather=true"
```

The first request should be a cache miss:

```text
X-Cache: MISS
```

Repeating the same request should produce a cache hit:

```text
X-Cache: HIT
```

### Using Redis

Start the proxy with Redis:

```bash
poetry run caching-proxy run \
  --origin https://api.open-meteo.com \
  --cache-backend redis \
  --redis-url redis://localhost:6379/0
```

---

# HTTP Caching

Caching decisions are based on the origin server's response headers rather than a single fixed TTL.

## Cache-Control

The proxy applies the following rules:

| Origin response      | Behavior                     |
| -------------------- | ---------------------------- |
| `no-store`           | Never cached                 |
| `no-cache`           | Never cached                 |
| `private`            | Never cached                 |
| `max-age=N`          | Cached for `N` seconds       |
| No caching directive | Uses configured fallback TTL |
| `Set-Cookie` present | Never cached                 |
| Non-`200` response   | Never cached                 |

The fallback TTL is controlled by `--cache-ttl` and defaults to `300` seconds. An explicit `max-age` from the origin takes precedence.

## Vary

`Vary` is handled conservatively.

### `Vary: *`

Responses containing:

```http
Vary: *
```

are never cached.

### `Vary: <headers>`

A response is cached only when every header named by `Vary` is included in the proxy's configured `--cache-vary-headers`.

This prevents the proxy from silently creating an incomplete cache-key scheme for headers that the operator has not explicitly configured.

For example:

```text
--cache-vary-headers Authorization Accept-Language
```

If the origin varies on another header that is not configured, the response is not cached.

## Client Cache-Control

Client cache directives are also respected:

* `Cache-Control: no-store` bypasses both cache reads and writes.
* `Cache-Control: no-cache` bypasses the cache read, while allowing the response to be cached for subsequent callers.

## Cache Keys

Configured vary headers are included in the cache key.

By default, `Authorization` is included. This prevents two callers using different credentials from sharing the same cached response.

---

# Response Headers

The proxy preserves repeated response headers.

This is important for headers such as:

```http
Set-Cookie: ...
Set-Cookie: ...
```

A naive conversion of response headers to a dictionary can collapse repeated headers into a single value. The proxy instead preserves legitimate repeated headers individually.

---

# Cache Backends

## Memory

The default backend is an in-process, bounded, TTL-aware cache.

```text
--cache-backend memory
```

Advantages:

* Simple
* Fast
* No external dependency

The cache is **per process**.

If multiple proxy instances run behind a load balancer, each instance has an independent cache:

```text
             Load Balancer
             /     |     \
            /      |      \
        Proxy A  Proxy B  Proxy C
          │         │        │
        Cache A   Cache B  Cache C
```

A request can therefore be a `HIT` on one replica and a `MISS` on another. This is an inherent property of the in-process backend.

## Redis

For horizontally scaled deployments, use Redis:

```bash
caching-proxy run \
  --origin https://api.open-meteo.com \
  --cache-backend redis \
  --redis-url redis://localhost:6379/0
```

All replicas then share the same logical cache.

Redis handles TTL through key expiration.

Cache-stampede protection uses a short-lived Redis lock:

```text
SET NX PX
```

The lock is deliberately simple rather than a full Redlock implementation.

If a request cannot acquire the lock quickly, it proceeds without waiting. The resulting worst case is an occasional duplicate origin request across replicas rather than a cache-correctness failure.

---

# TLS and Deployment

The proxy does not terminate TLS by default.

For production deployments, the recommended topology is:

```text
Client
  │
  │ HTTPS
  ▼
┌─────────────────────────────┐
│ Load Balancer / Reverse     │
│ Proxy / Service Mesh        │
│                             │
│ TLS termination             │
└──────────────┬──────────────┘
               │
               │ HTTP
               ▼
┌─────────────────────────────┐
│      Caching Proxy          │
└──────────────┬──────────────┘
               │
               │ HTTPS
               ▼
        Origin Server
```

For example:

```bash
caching-proxy run \
  --origin https://api.open-meteo.com \
  --trust-forwarded-headers
```

`--trust-forwarded-headers` allows the proxy to use forwarded client information when determining the real client IP for rate limiting.

### Security Warning

Only enable `--trust-forwarded-headers` when the proxy is behind a trusted intermediary and is not directly reachable by untrusted clients.

Otherwise, clients can spoof `X-Forwarded-For` and potentially bypass IP-based rate limiting. The option relies on Uvicorn's `proxy_headers` and `forwarded_allow_ips` support rather than implementing custom forwarded-header parsing.

### Direct TLS Termination

For simpler deployments, TLS can be terminated directly by Uvicorn:

```bash
caching-proxy run \
  --origin https://api.open-meteo.com \
  --tls-certfile /path/to/cert.pem \
  --tls-keyfile /path/to/key.pem
```

---

# Security

The proxy includes several protections intended to bound resource consumption and reduce common operational risks.

## Rate Limiting

Rate limiting is optional and disabled by default:

```bash
caching-proxy run \
  --origin https://api.open-meteo.com \
  --rate-limit \
  --rate-limit-requests 100 \
  --rate-limit-window 60
```

The default implementation uses an in-process fixed-window counter.

When the Redis backend is enabled, rate limiting is also backed by Redis so the limit is shared across replicas.

## Request Body Limits

Requests exceeding `--max-request-body-bytes` are rejected with:

```http
413 Payload Too Large
```

The default limit is 10 MB.

This prevents the proxy from buffering arbitrarily large request bodies in memory.

## Large Response Handling

Responses larger than `--max-buffered-response-bytes` are streamed directly to the client rather than buffered.

Such responses are not cached.

The default limit is 20 MB.

## Admin Authentication

Administrative token comparisons use `hmac.compare_digest` rather than ordinary string comparison to avoid timing-based leakage of matching token prefixes.

## Security Headers

The proxy adds baseline security headers:

```http
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Strict-Transport-Security: ...
```

`Strict-Transport-Security` is enabled when TLS is configured.

---

# Cache Invalidation

Cache invalidation operates against the running cache backend.

It does not delete local cache files because the cache is held in process memory or Redis.

Start the server with an administrative token:

```bash
caching-proxy run \
  --origin https://api.open-meteo.com \
  --admin-token secret
```

Then clear the cache:

```bash
caching-proxy clear-cache \
  --port 5000 \
  --admin-token secret
```

Without `--admin-token`, the cache-clear endpoint is disabled and returns `404`.

---

# Operational Endpoints

| Endpoint                      | Purpose            |
| ----------------------------- | ------------------ |
| `GET /_health`                | Liveness check     |
| `GET /_ready`                 | Readiness check    |
| `GET /metrics`                | Prometheus metrics |
| `POST /_internal/cache/clear` | Clear the cache    |

### Health

`/_health` only determines whether the process is running.

It does not check external dependencies. A temporarily unavailable Redis instance or origin therefore does not cause the liveness check to fail.

### Readiness

`/_ready` determines whether the instance can currently serve traffic.

For example, the Redis backend is checked when Redis is configured.

A failure returns:

```http
503 Service Unavailable
```

This allows an orchestrator to stop routing traffic to an unhealthy instance without necessarily terminating the process.

### Metrics

Prometheus metrics include:

```text
caching_proxy_requests_total
caching_proxy_upstream_errors_total
caching_proxy_request_duration_seconds
```

Metrics can be disabled with:

```bash
CACHING_PROXY_METRICS_ENABLED=false
```

There is currently no corresponding `run` CLI flag.

---

# Configuration

Every `run` option can also be configured through an environment variable using the `CACHING_PROXY_` prefix or through a `.env` file.

For example:

```bash
CACHING_PROXY_ORIGIN=https://api.open-meteo.com
CACHING_PROXY_CACHE_TTL_SECONDS=300
CACHING_PROXY_ADMIN_TOKEN=secret
```

CLI flags take precedence over environment variables.

## Configuration Reference

| Setting                   | CLI flag                                                       | Default  |
| ------------------------- | -------------------------------------------------------------- | -------- |
| Origin                    | `--origin`, `-o`                                               | Required |
| Port                      | `--port`, `-p`                                                 | `5000`   |
| Cache backend             | `--cache-backend`                                              | `memory` |
| Redis URL                 | `--redis-url`                                                  | —        |
| Fallback cache TTL        | `--cache-ttl`                                                  | `300s`   |
| Maximum cached entries    | `--cache-max-entries`                                          | `1000`   |
| Maximum cached item size  | Environment only: `CACHE_MAX_ITEM_BYTES`                       | `5 MB`   |
| Maximum buffered response | Environment only: `MAX_BUFFERED_RESPONSE_BYTES`                | `20 MB`  |
| Maximum request body      | Environment only: `MAX_REQUEST_BODY_BYTES`                     | `10 MB`  |
| Admin token               | `--admin-token`                                                | Disabled |
| Rate limiting             | `--rate-limit`, `--rate-limit-requests`, `--rate-limit-window` | Disabled |
| Forwarded headers         | `--trust-forwarded-headers`                                    | `false`  |
| TLS certificate           | `--tls-certfile`                                               | —        |
| TLS key                   | `--tls-keyfile`                                                | —        |
| Graceful shutdown         | `--graceful-shutdown-timeout`                                  | `10s`    |

---

# Development

Install development dependencies:

```bash
poetry install --with dev
```

Run the test suite:

```bash
poetry run pytest
```

Run linting:

```bash
poetry run ruff check .
```

Run type checking:

```bash
poetry run mypy app
```

CI runs the same checks across Python 3.13 and also builds the Docker image.

---

# Architecture

The project is organized around a small number of focused components:

```text
app/
├── cli.py
├── config.py
├── server.py
├── security.py
├── metrics.py
├── logging_config.py
│
├── cache/
│   ├── backend.py
│   ├── entry.py
│   ├── memory.py
│   ├── redis_backend.py
│   └── factory.py
│
└── proxy/
    ├── service.py
    ├── cache_control.py
    ├── headers.py
    ├── keys.py
    └── errors.py
```

### Application Layer

```text
cli.py
   │
   ▼
server.py
   │
   ├── configuration
   ├── middleware
   ├── routes
   └── application lifespan
           │
           ▼
      ProxyService
           │
      ┌────┴────┐
      ▼         ▼
   Cache     Origin
  Backend    Server
```

### Module Responsibilities

| Module                   | Responsibility                                                |
| ------------------------ | ------------------------------------------------------------- |
| `cli.py`                 | Typer CLI commands such as `run` and `clear-cache`            |
| `config.py`              | Application configuration and startup validation              |
| `server.py`              | FastAPI application factory, lifespan, middleware, and routes |
| `security.py`            | Rate limiting, token comparison, and security headers         |
| `metrics.py`             | Prometheus metrics                                            |
| `logging_config.py`      | Structured JSON logging                                       |
| `cache/backend.py`       | Cache backend protocol                                        |
| `cache/entry.py`         | Immutable cache entry representation                          |
| `cache/memory.py`        | In-process cache implementation                               |
| `cache/redis_backend.py` | Redis cache implementation                                    |
| `cache/factory.py`       | Cache backend construction                                    |
| `proxy/service.py`       | Core request forwarding and caching logic                     |
| `proxy/cache_control.py` | `Cache-Control` and `Vary` policy                             |
| `proxy/headers.py`       | HTTP header filtering and preservation                        |
| `proxy/keys.py`          | Cache-key construction                                        |
| `proxy/errors.py`        | Proxy-specific exceptions                                     |

---

# Design

The application avoids module-level mutable state.

Settings, the cache backend, and the shared `httpx.AsyncClient` are created once during the application's lifespan and explicitly passed into `ProxyService`.

This keeps dependencies explicit and makes the core proxy logic straightforward to test in isolation.

---

# Known Limitations

The following are intentional scope boundaries rather than undocumented omissions.

## Conditional Revalidation

The proxy does not currently perform conditional revalidation using:

```http
If-None-Match
If-Modified-Since
```

A stale response is fetched again from the origin rather than being conditionally revalidated.

Supporting this would require storing validators and correctly handling `304 Not Modified` responses.

## Redis Locking

Redis cache-stampede protection uses a simple:

```text
SET NX PX
```

lock rather than a full Redlock implementation.

It should therefore be viewed as stampede **reduction**, not a strict distributed mutual-exclusion mechanism.

## Redirect Rewriting

`Location` headers are forwarded unchanged.

If the origin and proxy use different public base URLs, following a redirect may take the client directly to the origin instead of back through the proxy.

## Origin Resilience

The proxy currently does not provide:

* HTTP/2 to the origin
* Automatic request retries
* Exponential backoff
* Circuit breaking

A failed origin request therefore results in a failed proxy response rather than an automatic retry.

## Query-String Normalization

Cache keys use the query string as provided.

Consequently:

```text
?a=1&b=2
```

and:

```text
?b=2&a=1
```

produce different cache entries even when the origin treats them equivalently.

---

# Project Status

The current implementation focuses on:

1. Correct HTTP caching behavior
2. Safe request and response handling
3. Memory and Redis cache backends
4. Horizontal deployment support
5. Operational visibility
6. Explicit dependency management
7. A deliberately constrained feature set

The limitations above identify areas that could be addressed in future iterations without changing the core architecture.
