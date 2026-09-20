from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """An immutable snapshot of a cached upstream response.

    Only the fields needed to faithfully replay the response are kept —
    notably we do NOT store hop-by-hop headers (see proxy/headers.py),
    since those describe the original transport, not the cached payload.
    """

    status_code: int
    headers: tuple[tuple[str, str], ...]
    content: bytes
    media_type: str | None