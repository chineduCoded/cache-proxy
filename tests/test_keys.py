from app.proxy.keys import build_cache_key


def test_same_inputs_produce_same_key():
    k1 = build_cache_key(
        method="GET", url="http://x/y", request_headers={}, vary_headers=("authorization",)
    )
    k2 = build_cache_key(
        method="GET", url="http://x/y", request_headers={}, vary_headers=("authorization",)
    )
    assert k1 == k2


def test_different_vary_header_values_produce_different_keys():
    """This is the fix for cross-user cache poisoning: two callers with
    different Authorization headers must never collide on the same key.
    """
    k1 = build_cache_key(
        method="GET",
        url="http://x/y",
        request_headers={"Authorization": "Bearer user-a"},
        vary_headers=("authorization",),
    )
    k2 = build_cache_key(
        method="GET",
        url="http://x/y",
        request_headers={"Authorization": "Bearer user-b"},
        vary_headers=("authorization",),
    )
    assert k1 != k2


def test_missing_vary_header_is_treated_consistently():
    k1 = build_cache_key(
        method="GET", url="http://x/y", request_headers={}, vary_headers=("authorization",)
    )
    k2 = build_cache_key(
        method="GET",
        url="http://x/y",
        request_headers={"Accept": "text/html"},
        vary_headers=("authorization",),
    )
    assert k1 == k2  # neither request carries Authorization


def test_different_urls_produce_different_keys():
    k1 = build_cache_key(method="GET", url="http://x/a", request_headers={}, vary_headers=())
    k2 = build_cache_key(method="GET", url="http://x/b", request_headers={}, vary_headers=())
    assert k1 != k2
