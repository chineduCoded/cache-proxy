import hashlib


def get_cached_key(url: str) -> str:
    return hashlib.blake2b(url.encode()).hexdigest()