from starlette.responses import Response

from app.proxy.headers import (
    apply_headers,
    filter_request_headers,
    filter_response_headers,
)


def test_request_headers_strip_host_and_hop_by_hop():
    headers = [
        ("Host", "proxy.local"),
        ("Authorization", "Bearer token"),
        ("Connection", "keep-alive"),
        ("Accept", "application/json"),
    ]
    result = dict(filter_request_headers(headers))
    assert "host" not in {k.lower() for k in result}
    assert "connection" not in {k.lower() for k in result}
    assert result["Authorization"] == "Bearer token"
    assert result["Accept"] == "application/json"


def test_response_headers_strip_content_encoding_and_length():
    """httpx has already decompressed the body by the time we see
    `.content`, so a stale Content-Encoding/Content-Length would corrupt
    the response for the actual client.
    """
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Encoding", "gzip"),
        ("Content-Length", "1234"),
        ("X-Custom", "value"),
    ]
    result = dict(filter_response_headers(headers))
    lowered = {k.lower() for k in result}
    assert "content-encoding" not in lowered
    assert "content-length" not in lowered
    assert result["Content-Type"] == "application/json"
    assert result["X-Custom"] == "value"


def test_response_headers_preserve_duplicates():
    """The historical bug this guards against: `dict(response.headers)`
    (and even httpx's own `.items()`) silently collapses repeated header
    names (e.g. two Set-Cookie headers) down to just one comma-joined
    value.
    """
    headers = [
        ("Set-Cookie", "session=abc; Path=/"),
        ("Set-Cookie", "csrf=xyz; Path=/"),
        ("Content-Type", "text/html"),
    ]
    result = filter_response_headers(headers)
    set_cookie_values = [v for k, v in result if k.lower() == "set-cookie"]
    assert set_cookie_values == ["session=abc; Path=/", "csrf=xyz; Path=/"]


def test_apply_headers_preserves_duplicates_on_response():
    response = Response(content=b"ok")
    apply_headers(
        response,
        [
            ("Set-Cookie", "a=1"),
            ("Set-Cookie", "b=2"),
            ("X-Custom", "value"),
        ],
    )
    raw_set_cookie = [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]
    assert raw_set_cookie == ["a=1", "b=2"]
    assert response.headers["x-custom"] == "value"
