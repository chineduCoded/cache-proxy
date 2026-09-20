import httpx
from starlette.testclient import TestClient

from app.config import build_settings
from app.server import create_app


def make_client(**overrides) -> TestClient:
    settings = build_settings(origin="http://origin.example", port=5000, **overrides)
    app = create_app(settings)
    return TestClient(app)


def fake_stream_response(
    status_code: int = 200,
    headers: dict | list[tuple[str, str]] | None = None,
    body: bytes = b"{}",
):
    """Builds a fake async context manager mimicking `httpx.AsyncClient.stream()`."""

    class _FakeStream:
        def __init__(self):
            self.status_code = status_code
            # Accept both dict and list-of-tuples so multi-value headers work
            self.headers = httpx.Headers(headers or {})

        async def aiter_bytes(self):
            yield body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    return _FakeStream()


def patch_stream(monkeypatch, handler):
    """
    handler(method, url, headers, content, timeout) -> fake stream object

    All test handlers must accept the 5-argument signature (timeout is
    usually ignored).
    """

    def fake_stream(self, method, url, *, headers=None, content=None, timeout=None, **kwargs):
        return handler(method, url, headers, content, timeout)

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_get_is_cache_miss_then_hit(monkeypatch):
    calls = {"count": 0}

    def handler(method, url, headers, content, timeout):
        calls["count"] += 1
        return fake_stream_response(
            200,
            {"Content-Type": "application/json"},
            b'{"message": "hi"}',
        )

    patch_stream(monkeypatch, handler)

    with make_client() as client:
        r1 = client.get("/some-path")
        assert r1.status_code == 200
        assert r1.headers["X-Cache"] == "MISS"
        assert r1.json() == {"message": "hi"}

        r2 = client.get("/some-path")
        assert r2.status_code == 200
        assert r2.headers["X-Cache"] == "HIT"
        assert r2.json() == {"message": "hi"}

    assert calls["count"] == 1


def test_non_get_requests_are_forwarded_not_404(monkeypatch):
    def handler(method, url, headers, content, timeout):
        assert method == "POST"
        assert content == b'{"n": 1}'
        return fake_stream_response(
            201, {"Content-Type": "application/json"}, b'{"created": true}'
        )

    patch_stream(monkeypatch, handler)

    with make_client() as client:
        r = client.post("/items", content=b'{"n": 1}')
        assert r.status_code == 201
        assert r.json() == {"created": True}


def test_post_responses_are_never_cached(monkeypatch):
    calls = {"count": 0}

    def handler(method, url, headers, content, timeout):
        calls["count"] += 1
        return fake_stream_response(200, {"Content-Type": "application/json"}, b"{}")

    patch_stream(monkeypatch, handler)

    with make_client() as client:
        client.post("/items")
        client.post("/items")

    assert calls["count"] == 2


def test_cross_user_responses_are_not_shared(monkeypatch):
    def handler(method, url, headers, content, timeout):
        who = (headers or {}).get("authorization", "anon")
        return fake_stream_response(
            200,
            {"Content-Type": "application/json"},
            f'{{"user": "{who}"}}'.encode(),
        )

    patch_stream(monkeypatch, handler)

    with make_client() as client:
        r_a = client.get("/me", headers={"Authorization": "user-a"})
        r_b = client.get("/me", headers={"Authorization": "user-b"})

        assert r_a.json() == {"user": "user-a"}
        assert r_b.json() == {"user": "user-b"}
        assert r_a.headers["X-Cache"] == "MISS"
        assert r_b.headers["X-Cache"] == "MISS"


def test_cache_control_no_store_is_not_cached(monkeypatch):
    calls = {"count": 0}

    def handler(method, url, headers, content, timeout):
        calls["count"] += 1
        return fake_stream_response(
            200,
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
            b"{}",
        )

    patch_stream(monkeypatch, handler)

    with make_client() as client:
        client.get("/no-store")
        client.get("/no-store")

    assert calls["count"] == 2


def test_cache_control_max_age_is_honored(monkeypatch):
    def handler(method, url, headers, content, timeout):
        return fake_stream_response(
            200,
            {"Content-Type": "application/json", "Cache-Control": "max-age=1000"},
            b"{}",
        )

    patch_stream(monkeypatch, handler)

    with make_client() as client:
        r = client.get("/short-ttl")
        assert r.headers["X-Cache"] == "MISS"
        r2 = client.get("/short-ttl")
        assert r2.headers["X-Cache"] == "HIT"


def test_multiple_set_cookie_headers_are_all_forwarded(monkeypatch):
    def handler(method, url, headers, content, timeout):
        return fake_stream_response(
            200,
            [
                ("Content-Type", "application/json"),
                ("Set-Cookie", "session=abc"),
                ("Set-Cookie", "csrf=xyz"),
            ],
            b"{}",
        )

    patch_stream(monkeypatch, handler)

    with make_client() as client:
        r = client.get("/login")
        all_cookies = [
            v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"
        ]
        assert all_cookies == ["session=abc", "csrf=xyz"]


def test_upstream_connection_error_returns_502(monkeypatch):
    def handler(method, url, headers, content, timeout):
        class _Raiser:
            async def __aenter__(self):
                raise httpx.ConnectError("boom")

            async def __aexit__(self, *exc):
                return False

        return _Raiser()

    patch_stream(monkeypatch, handler)

    with make_client() as client:
        r = client.get("/anything")
        assert r.status_code == 502


def test_upstream_timeout_returns_504(monkeypatch):
    def handler(method, url, headers, content, timeout):
        class _Raiser:
            async def __aenter__(self):
                raise httpx.ReadTimeout("too slow")

            async def __aexit__(self, *exc):
                return False

        return _Raiser()

    patch_stream(monkeypatch, handler)

    with make_client() as client:
        r = client.get("/anything")
        assert r.status_code == 504


def test_request_body_over_limit_returns_413(monkeypatch):
    def handler(method, url, headers, content, timeout):
        return fake_stream_response(200, {"Content-Type": "application/json"}, b"{}")

    patch_stream(monkeypatch, handler)

    with make_client(max_request_body_bytes=10) as client:
        r = client.post("/upload", content=b"x" * 100)
        assert r.status_code == 413


def test_large_response_is_streamed_and_not_cached(monkeypatch):
    big_body = b"x" * 1000
    calls = {"count": 0}

    def handler(method, url, headers, content, timeout):
        calls["count"] += 1
        return fake_stream_response(
            200, {"Content-Type": "application/octet-stream"}, big_body
        )

    patch_stream(monkeypatch, handler)

    with make_client(max_buffered_response_bytes=100) as client:
        r1 = client.get("/big")
        assert r1.status_code == 200
        assert r1.content == big_body

        r2 = client.get("/big")
        assert r2.content == big_body

    assert calls["count"] == 2  # never cached


def test_health_endpoint():
    with make_client() as client:
        r = client.get("/_health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_ready_endpoint_reports_cache_status():
    with make_client() as client:
        r = client.get("/_ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["cache_backend"] == "memory"


def test_metrics_endpoint_exposes_prometheus_format(monkeypatch):
    def handler(method, url, headers, content, timeout):
        return fake_stream_response(200, {"Content-Type": "application/json"}, b"{}")

    patch_stream(monkeypatch, handler)

    with make_client() as client:
        client.get("/warm-up-a-metric")
        r = client.get("/metrics")
        assert r.status_code == 200
        assert b"caching_proxy_requests_total" in r.content


def test_metrics_route_absent_falls_through_to_proxy(monkeypatch):
    def handler(method, url, headers, content, timeout):
        assert str(url).endswith("/metrics")
        return fake_stream_response(200, {"Content-Type": "application/json"}, b"{}")

    patch_stream(monkeypatch, handler)

    with make_client(metrics_enabled=False) as client:
        r = client.get("/metrics")
        assert r.status_code == 200


def test_admin_endpoint_disabled_by_default():
    with make_client(admin_token=None) as client:
        r = client.post("/_internal/cache/clear")
        assert r.status_code == 404


def test_admin_endpoint_requires_correct_token():
    with make_client(admin_token="secret") as client:
        r = client.post(
            "/_internal/cache/clear", headers={"X-Admin-Token": "wrong"}
        )
        assert r.status_code == 403

        r = client.post(
            "/_internal/cache/clear", headers={"X-Admin-Token": "secret"}
        )
        assert r.status_code == 200


def test_rate_limit_blocks_after_threshold(monkeypatch):
    def handler(method, url, headers, content, timeout):
        return fake_stream_response(200, {"Content-Type": "application/json"}, b"{}")

    patch_stream(monkeypatch, handler)

    with make_client(
        rate_limit_enabled=True,
        rate_limit_requests=2,
        rate_limit_window_seconds=60,
    ) as client:
        assert client.get("/a").status_code == 200
        assert client.get("/a").status_code == 200
        assert client.get("/a").status_code == 429


def test_rate_limit_exempts_health_endpoint():
    with make_client(
        rate_limit_enabled=True,
        rate_limit_requests=1,
        rate_limit_window_seconds=60,
    ) as client:
        for _ in range(5):
            assert client.get("/_health").status_code == 200


def test_security_headers_present():
    with make_client() as client:
        r = client.get("/_health")
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("referrer-policy") == "no-referrer"