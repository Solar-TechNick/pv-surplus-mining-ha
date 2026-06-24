"""Error hierarchy (vendored from the adapter; HTTP status codes dropped)."""


class AdapterError(Exception):
    """Base class for controller errors."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConfigError(AdapterError):
    pass


class OutOfRangeError(AdapterError):
    pass


class RateLimitedError(AdapterError):
    pass


class MinerUnavailableError(AdapterError):
    pass


class AuthError(AdapterError):
    pass


class UpstreamError(AdapterError):
    pass
