from app.proxy.cache_control import (
    request_forbids_cache_read,
    request_forbids_cache_write,
    response_cache_policy,
)


def test_no_directive_falls_back_to_default_ttl():
    policy = response_cache_policy(
        status_code=200,
        response_headers=[("Content-Type", "application/json")],
        default_ttl_seconds=300,
        configured_vary_headers=(),
    )
    assert policy.cacheable
    assert policy.ttl_seconds == 300


def test_max_age_overrides_default_ttl():
    policy = response_cache_policy(
        status_code=200,
        response_headers=[("Cache-Control", "max-age=42")],
        default_ttl_seconds=300,
        configured_vary_headers=(),
    )
    assert policy.cacheable
    assert policy.ttl_seconds == 42


def test_no_store_is_not_cacheable():
    policy = response_cache_policy(
        status_code=200,
        response_headers=[("Cache-Control", "no-store")],
        default_ttl_seconds=300,
        configured_vary_headers=(),
    )
    assert not policy.cacheable


def test_private_is_not_cacheable():
    policy = response_cache_policy(
        status_code=200,
        response_headers=[("Cache-Control", "private, max-age=60")],
        default_ttl_seconds=300,
        configured_vary_headers=(),
    )
    assert not policy.cacheable


def test_non_200_is_not_cacheable():
    policy = response_cache_policy(
        status_code=404,
        response_headers=[],
        default_ttl_seconds=300,
        configured_vary_headers=(),
    )
    assert not policy.cacheable


def test_set_cookie_is_not_cacheable():
    policy = response_cache_policy(
        status_code=200,
        response_headers=[("Set-Cookie", "session=abc")],
        default_ttl_seconds=300,
        configured_vary_headers=(),
    )
    assert not policy.cacheable


def test_vary_star_is_never_cacheable():
    policy = response_cache_policy(
        status_code=200,
        response_headers=[("Vary", "*")],
        default_ttl_seconds=300,
        configured_vary_headers=("authorization",),
    )
    assert not policy.cacheable


def test_vary_within_configured_headers_is_cacheable():
    policy = response_cache_policy(
        status_code=200,
        response_headers=[("Vary", "Authorization")],
        default_ttl_seconds=300,
        configured_vary_headers=("authorization",),
    )
    assert policy.cacheable


def test_vary_outside_configured_headers_is_not_cacheable():
    """Safety-first design choice: rather than guessing at an expanded
    cache-key scheme for a header the operator hasn't configured, refuse
    to cache at all.
    """
    policy = response_cache_policy(
        status_code=200,
        response_headers=[("Vary", "Accept-Language")],
        default_ttl_seconds=300,
        configured_vary_headers=("authorization",),
    )
    assert not policy.cacheable


def test_request_no_store_forbids_read_and_write():
    headers = {"cache-control": "no-store"}
    assert request_forbids_cache_read(headers)
    assert request_forbids_cache_write(headers)


def test_request_no_cache_forbids_read_only():
    headers = {"cache-control": "no-cache"}
    assert request_forbids_cache_read(headers)
    assert not request_forbids_cache_write(headers)


def test_request_with_no_cache_control_allows_both():
    assert not request_forbids_cache_read({})
    assert not request_forbids_cache_write({})
