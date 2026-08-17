from __future__ import annotations


class ExtractionError(RuntimeError):
    """Base class for expected extraction failures."""


class ConfigurationError(ExtractionError, ValueError):
    """Raised when caller-supplied configuration is invalid."""


class NetworkError(ExtractionError):
    """Raised when a request fails before an HTTP response is received."""

    def __init__(self, stage: str, detail: str):
        self.stage = str(stage or "request")
        self.detail = str(detail or "network request failed")
        super().__init__(f"{self.stage}: {self.detail}")


class ProtocolError(ExtractionError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ProviderRequiresApproval(ExtractionError):
    pass


class ExtractionCancelled(ExtractionError):
    """Raised when a cooperative task cancellation is observed."""


# Backward-compatible name for code that used the old exception spelling.
PaypalRequiresApproval = ProviderRequiresApproval
