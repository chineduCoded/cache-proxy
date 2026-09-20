class RequestBodyTooLarge(Exception):
    """Raised internally when a request body exceeds
    `Settings.max_request_body_bytes`. Never escapes `ProxyService` —
    it's translated into a 413 response.
    """