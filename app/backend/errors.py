class MongoUnavailableError(RuntimeError):
    """Raised when a resource operation cannot reach MongoDB."""


class DuplicateResourceError(RuntimeError):
    """Raised when a unique resource already exists."""


class ResourceNotFoundError(RuntimeError):
    """Raised when a requested resource does not exist."""


class InsufficientEmailsError(RuntimeError):
    """Raised when a run requests more emails than are available."""


class ProxyCountryUnavailableError(RuntimeError):
    """Raised when the selected registration country has too few enabled proxies."""

    def __init__(self, country: str, required: int, available: int) -> None:
        self.country = country
        self.required = required
        self.available = available
        super().__init__(
            f"注册国家 {country} 的可用代理不足：需要 {required}，当前 {available}"
        )


class RunConflictError(RuntimeError):
    """Raised when another run is already active."""


class RunNotFoundError(RuntimeError):
    """Raised when a persisted run cannot be found."""


class RoxyWorkspaceMissingError(RuntimeError):
    """Raised before reservation when Roxy has no workspace container."""

    def __init__(self) -> None:
        super().__init__("Roxy 中没有可用 workspace，请先创建一个 workspace")
