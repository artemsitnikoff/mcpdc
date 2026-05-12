"""Custom exceptions for Confluence MCP server."""


class ConfluenceError(Exception):
    """Base exception for Confluence-related errors."""

    def __init__(self, message: str, status_code: int | None = None):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ConfluenceNotFoundError(ConfluenceError):
    """Raised when a Confluence resource is not found."""

    def __init__(self, resource_type: str, identifier: str):
        message = f"{resource_type} not found: {identifier}"
        super().__init__(message, 404)


class ConfluencePermissionError(ConfluenceError):
    """Raised when user lacks permissions for an operation."""

    def __init__(self, message: str):
        super().__init__(f"Permission denied: {message}", 403)


class ConfluenceValidationError(ConfluenceError):
    """Raised when request validation fails."""

    def __init__(self, field: str, reason: str):
        message = f"Validation error for {field}: {reason}"
        super().__init__(message, 400)


class ConfluenceVersionConflictError(ConfluenceError):
    """Raised when version conflict occurs during update.

    Server-reported `current_version` / `provided_version` are best-effort —
    Confluence may not always include them in the error body.
    """

    def __init__(
        self,
        message: str,
        current_version: int | None = None,
        provided_version: int | None = None,
    ):
        self.current_version = current_version
        self.provided_version = provided_version
        super().__init__(message, 409)


class ConfluenceAuthError(ConfluenceError):
    """Raised on 401 Unauthorized — credentials wrong, expired, or CAPTCHA-locked."""

    def __init__(self, message: str = "Unauthorized — check credentials or CAPTCHA lock"):
        super().__init__(message, 401)


class ConfluenceRateLimitError(ConfluenceError):
    """Raised on 429 Too Many Requests."""

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message, 429)


class ConfluenceSizeLimitError(ConfluenceError):
    """Raised when an upload or download exceeds the configured byte limit."""

    def __init__(self, size: int, limit: int):
        self.size = size
        self.limit = limit
        super().__init__(f"Payload size {size} bytes exceeds limit of {limit} bytes")


class ConfluencePathError(ConfluenceError):
    """Raised when a download path escapes the configured download directory."""

    def __init__(self, requested: str, base: str):
        self.requested = requested
        self.base = base
        super().__init__(f"Path {requested!r} escapes download directory {base!r}")
