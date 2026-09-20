from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)


class Metrics:
    """A per-app metrics registry (not a global singleton), so multiple
    `create_app()` calls in the same process — as happens across the test
    suite — don't collide registering the same metric name twice.
    """

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests_total = Counter(
            "caching_proxy_requests_total",
            "Requests handled, by method and cache status",
            ["method", "cache_status"],
            registry=self.registry,
        )
        self.upstream_errors_total = Counter(
            "caching_proxy_upstream_errors_total",
            "Errors reaching the origin, by kind",
            ["kind"],
            registry=self.registry,
        )
        self.request_duration_seconds = Histogram(
            "caching_proxy_request_duration_seconds",
            "End-to-end request handling time",
            ["method"],
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


__all__ = ["CONTENT_TYPE_LATEST", "Metrics"]