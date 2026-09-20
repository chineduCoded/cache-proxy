from collections.abc import Iterable

from starlette.responses import Response

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

HeaderPairs = list[tuple[str, str]]


def filter_request_headers(headers: Iterable[tuple[str, str]]) -> HeaderPairs:
    return [(k, v) for k, v in headers if k.lower() not in _STRIP_FROM_REQUEST]


def filter_response_headers(headers: Iterable[tuple[str, str]]) -> HeaderPairs:
    """Filter hop-by-hop headers while preserving duplicates.

    Deliberately NOT dict-based: a plain ``dict(response.headers)`` (and
    even httpx's own ``.items()``) silently collapses repeated header
    names to a single comma-joined value, which is wrong for anything
    that legitimately appears more than once — most commonly multiple
    ``Set-Cookie`` headers (e.g. a session cookie plus a CSRF cookie).
    Callers must pass ``upstream.headers.multi_items()`` (not ``.items()``)
    to actually get the duplicates in the first place.
    """
    return [(k, v) for k, v in headers if k.lower() not in _STRIP_ALWAYS]

def apply_headers(response: Response, header_pairs: Iterable[tuple[str, str]]) -> None:
    """Append every header pair onto a Starlette Response, preserving
    duplicates (``MutableHeaders.append`` adds rather than overwrites).
    """
    for key, value in header_pairs:
        response.headers.append(key, value)