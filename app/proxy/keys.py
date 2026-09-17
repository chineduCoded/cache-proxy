from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping


def build_cache_key(
    *,
    method: str,
    url: str,
    request_headers: Mapping[str, str],
    vary_headers: Iterable[str],
) -> str:
    """Build a cache key from the method, URL, and the values of any
    configured "vary" headers present on the request.

    Including vary headers (default: ``authorization``) is what stops one
    caller's cached response from being replayed to a different caller —
    without it, two users hitting the same path with different bearer
    tokens would silently receive each other's data once either response
    was cached.
    """
    lowered = {k.lower(): v for k, v in request_headers.items()}
    vary_component = "|".join(
        f"{name.lower()}={lowered.get(name.lower(), '')}" for name in vary_headers
    )
    raw = f"{method.upper()}\n{url}\n{vary_component}"
    return hashlib.blake2b(raw.encode(), digest_size=32).hexdigest()