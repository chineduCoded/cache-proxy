from app.proxy.headers import filter_request_headers, filter_response_headers


def test_request_headers_strip_host_and_hop_by_hop():
    headers = {
        "Host": "proxy.local",
        "Authorization": "Bearer token",
        "Connection": "keep-alive",
        "Accept": "application/json",
    }
    result = filter_request_headers(headers)
    assert "host" not in {k.lower() for k in result}
    assert "connection" not in {k.lower() for k in result}
    assert result["Authorization"] == "Bearer token"
    assert result["Accept"] == "application/json"


def test_response_headers_strip_content_encoding_and_length():
    """httpx has already decompressed the body by the time we see
    `.content`, so a stale Content-Encoding/Content-Length would corrupt
    the response for the actual client.
    """
    headers = {
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
        "Content-Length": "1234",
        "X-Custom": "value",
    }
    result = filter_response_headers(headers)
    lowered = {k.lower() for k in result}
    assert "content-encoding" not in lowered
    assert "content-length" not in lowered
    assert result["Content-Type"] == "application/json"
    assert result["X-Custom"] == "value"
