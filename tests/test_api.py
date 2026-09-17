import httpx
from starlette.testclient import TestClient

from app.config import build_settings
from app.server import create_app


def make_client(**overrides) -> TestClient:
    settings = build_settings(origin="http://origin.example", port=5000, **overrides)
    app = create_app(settings)
    return TestClient(app)


def test_get_is_cache_miss_then_hit(monkeypatch):
    calls = {"count": 0}

    async def fake_request(self, method, url, headers=None, content=None, timeout=None):
        calls["count"] += 1
        return httpx.Response(
            200,
            content=b'{"message": "hi"}',
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    with make_client() as client:
        r1 = client.get("/some-path")
        assert r1.status_code == 200
        assert r1.headers["X-Cache"] == "MISS"
        assert r1.json() == {"message": "hi"}

        r2 = client.get("/some-path")
        assert r2.status_code == 200
        assert r2.headers["X-Cache"] == "HIT"
        assert r2.json() == {"message": "hi"}

    # Origin was only actually hit once.
    assert calls["count"] == 1


def test_non_get_requests_are_forwarded_not_404(monkeypatch):
    """Regression test: the original middleware-based proxy only handled
    GET, so POST/PUT/DELETE always 404'd."""

    async def fake_request(self, method, url, headers=None, content=None, timeout=None):
        assert method == "POST"
        assert content == b'{"n": 1}'
        return httpx.Response(201, content=b'{"created": true}',
                               headers={"Content-Type": "application/json"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    with make_client() as client:
        r = client.post("/items", content=b'{"n": 1}')
        assert r.status_code == 201
        assert r.json() == {"created": True}


def test_post_responses_are_never_cached(monkeypatch):
    calls = {"count": 0}

    async def fake_request(self, method, url, headers=None, content=None, timeout=None):
        calls["count"] += 1
        return httpx.Response(200, content=b"{}", headers={"Content-Type": "application/json"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    with make_client() as client:
        client.post("/items")
        client.post("/items")

    assert calls["count"] == 2


def test_cross_user_responses_are_not_shared(monkeypatch):
    """Two different bearer tokens hitting the same path must never see
    each other's cached response."""

    async def fake_request(self, method, url, headers=None, content=None, timeout=None):
        who = (headers or {}).get("authorization", "anon")
        return httpx.Response(
            200,
            content=f'{{"user": "{who}"}}'.encode(),
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    with make_client() as client:
        r_a = client.get("/me", headers={"Authorization": "user-a"})
        r_b = client.get("/me", headers={"Authorization": "user-b"})

        assert r_a.json() == {"user": "user-a"}
        assert r_b.json() == {"user": "user-b"}
        assert r_a.headers["X-Cache"] == "MISS"
        assert r_b.headers["X-Cache"] == "MISS"  # not served user-a's cached entry


def test_upstream_connection_error_returns_502(monkeypatch):
    async def fake_request(self, method, url, headers=None, content=None, timeout=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    with make_client() as client:
        r = client.get("/anything")
        assert r.status_code == 502


def test_upstream_timeout_returns_504(monkeypatch):
    async def fake_request(self, method, url, headers=None, content=None, timeout=None):
        raise httpx.ReadTimeout("too slow")

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    with make_client() as client:
        r = client.get("/anything")
        assert r.status_code == 504


def test_health_endpoint():
    with make_client() as client:
        r = client.get("/_health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_admin_endpoint_disabled_by_default():
    with make_client(admin_token=None) as client:
        r = client.post("/_internal/cache/clear")
        assert r.status_code == 404


def test_admin_endpoint_requires_correct_token():
    with make_client(admin_token="secret") as client:
        r = client.post("/_internal/cache/clear", headers={"X-Admin-Token": "wrong"})
        assert r.status_code == 403

        r = client.post("/_internal/cache/clear", headers={"X-Admin-Token": "secret"})
        assert r.status_code == 200