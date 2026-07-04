"""
Custom exceptions cho VietAuto-style REST API server.
"""


class VietAutoAPIError(Exception):
    """Base exception cho tất cả lỗi API."""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def to_dict(self):
        return {"success": False, "error": self.message, "code": self.status_code}


class AuthenticationError(VietAutoAPIError):
    """401 - API key không hợp lệ hoặc session hết hạn."""

    def __init__(self, message: str = "Invalid or expired API key"):
        super().__init__(message, 401)


class CookieExpiredError(VietAutoAPIError):
    """403 - Google Labs cookie đã hết hạn, cần refresh."""

    def __init__(
        self, message: str = "Google session cookie has expired. Please update cookie."
    ):
        super().__init__(message, 403)


class InsufficientCreditsError(VietAutoAPIError):
    """402 - Không đủ credit."""

    def __init__(self, message: str = "Insufficient credits"):
        super().__init__(message, 402)


class RateLimitError(VietAutoAPIError):
    """429 - Quá nhiều request."""

    def __init__(self, message: str = "Rate limit exceeded. Please slow down."):
        super().__init__(message, 429)


class TaskTimeoutError(VietAutoAPIError):
    """Polling task quá lâu không hoàn thành."""

    def __init__(self, task_id: str, timeout: int):
        super().__init__(
            f"Task '{task_id}' did not complete within {timeout} seconds.", 504
        )
        self.task_id = task_id
        self.timeout = timeout


class TaskNotFoundError(VietAutoAPIError):
    """Task không tồn tại."""

    def __init__(self, task_id: str):
        super().__init__(f"Task '{task_id}' not found.", 404)
        self.task_id = task_id


class ProjectNotFoundError(VietAutoAPIError):
    """Project không tồn tại."""

    def __init__(self, project_id: str):
        super().__init__(f"Project '{project_id}' not found.", 404)
        self.project_id = project_id


class ValidationError(VietAutoAPIError):
    """400 - Dữ liệu đầu vào không hợp lệ."""

    def __init__(self, message: str):
        super().__init__(message, 400)


class UpstreamAPIError(VietAutoAPIError):
    """502 - Lỗi từ Google/upstream API."""

    def __init__(self, message: str, upstream_status: int = None):
        full_msg = f"Upstream API error: {message}"
        if upstream_status:
            full_msg += f" (HTTP {upstream_status})"
        super().__init__(full_msg, 502)
        self.upstream_status = upstream_status
