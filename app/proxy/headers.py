from __future__ import annotations

# RFC 7230 §6.1 hop-by-hop headers, plus headers that describe a transport
# encoding httpx has already undone by the time we see `response.content`
# (httpx auto-decompresses gzip/deflate/br) or that we must recompute
# ourselves because the body has changed shape (Content-Length).
_STRIP_ALWAYS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}

# Additionally stripped only when forwarding the *client's* request headers
# to the origin — "host" must reflect the origin, not the proxy.
_STRIP_FROM_REQUEST = _STRIP_ALWAYS | {"host"}


def filter_request_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _STRIP_FROM_REQUEST}


def filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _STRIP_ALWAYS}