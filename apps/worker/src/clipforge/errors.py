from __future__ import annotations


class AppError(Exception):
    """Error that is safe to show to the user.

    ``code`` is a stable machine identifier the UI can branch on.
    ``message`` must never contain paths, commands or stack traces.
    """

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details or {}

    def to_payload(self) -> dict:
        payload = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class Unsupported(AppError):
    status_code = 415
    code = "unsupported_media"


class CapacityExceeded(AppError):
    status_code = 507
    code = "insufficient_disk"


class DependencyMissing(AppError):
    status_code = 503
    code = "dependency_missing"


class ProviderUnavailable(AppError):
    status_code = 503
    code = "provider_unavailable"


class JobCancelled(Exception):
    """Raised inside a job handler when the user asks to cancel."""


class JobFailed(Exception):
    """Raised by a handler with a user-safe code/message pair."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
