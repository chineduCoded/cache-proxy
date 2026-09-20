from dataclasses import dataclass


def _parse_cache_control(header_value: str) -> dict[str, str | bool]:
    """Parse a Cache-Control header into a {directive: value_or_True} dict.

    Not a full RFC 7234 grammar parser (no quoted-string edge cases), but
    covers every directive this proxy actually acts on.
    """
    directives: dict[str, str | bool] = {}
    for part in header_value.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, _, value = part.partition("=")
            directives[name.strip().lower()] = value.strip().strip('"')
        else:
            directives[part.lower()] = True
    return directives


@dataclass(frozen=True)
class CachePolicy:
    cacheable: bool
    ttl_seconds: float
    reason: str = ""


def request_forbids_cache_read(request_headers: dict[str, str]) -> bool:
    """True if the client asked to bypass any cached copy for this request
    (Cache-Control: no-store or no-cache). Per RFC 7234, neither directive
    prevents *storing* a fresh response for other callers — only no-store
    does that (see `request_forbids_cache_write`).
    """
    raw = request_headers.get("cache-control", "")
    directives = _parse_cache_control(raw)
    return "no-store" in directives or "no-cache" in directives

def request_forbids_cache_write(request_headers: dict[str, str]) -> bool:
    """True only for Cache-Control: no-store — the response must not be
    stored at all, for anyone.
    """
    directives = _parse_cache_control(request_headers.get("cache-control", ""))
    return "no-store" in directives


def response_cache_policy(
    *,
    status_code: int,
    response_headers: list[tuple[str, str]],
    default_ttl_seconds: float,
    configured_vary_headers: tuple[str, ...],
) -> CachePolicy:
    """Decide whether — and for how long — an upstream response may be
    cached, honoring the origin's own Cache-Control and Vary headers.

    Deliberately conservative on ``Vary``: rather than trying to expand the
    cache-key scheme dynamically for every header an origin happens to vary
    on (which risks silently reusing this proxy's fixed vary-header set
    where it doesn't apply), a response that varies on anything **not**
    already in `cache_vary_headers` is simply not cached. If an origin is
    known to vary on a particular header, add it to `cache_vary_headers`
    rather than relying on this proxy to infer it.
    """
    lowered = {k.lower(): v for k, v in response_headers}

    if status_code != 200:
        return CachePolicy(False, 0, "non-200 response")

    if "set-cookie" in lowered:
        return CachePolicy(False, 0, "response carries Set-Cookie")

    vary = lowered.get("vary")
    if vary is not None:
        if vary.strip() == "*":
            return CachePolicy(False, 0, "Vary: * (never safely cacheable)")
        varies_on = {h.strip().lower() for h in vary.split(",") if h.strip()}
        configured = {h.lower() for h in configured_vary_headers}
        unhandled = varies_on - configured
        if unhandled:
            return CachePolicy(
                False, 0, f"response varies on unconfigured header(s): {sorted(unhandled)}"
            )

    cache_control = _parse_cache_control(lowered.get("cache-control", ""))
    if "no-store" in cache_control:
        return CachePolicy(False, 0, "Cache-Control: no-store")
    if "no-cache" in cache_control:
        return CachePolicy(False, 0, "Cache-Control: no-cache")
    if "private" in cache_control:
        return CachePolicy(False, 0, "Cache-Control: private")

    max_age = cache_control.get("s-maxage") or cache_control.get("max-age")
    if max_age is not None:
        try:
            ttl = float(max_age)
        except ValueError:
            ttl = default_ttl_seconds
        if ttl <= 0:
            return CachePolicy(False, 0, "Cache-Control: max-age<=0")
        return CachePolicy(True, ttl, "Cache-Control max-age")

    return CachePolicy(True, default_ttl_seconds, "no explicit directive, using default TTL")