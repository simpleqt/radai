"""Application-level errors with stable HTTP semantics.

Domain and infrastructure code should raise these errors instead of importing
FastAPI.  The API layer is responsible for rendering them as HTTP responses.
"""

from __future__ import annotations


class ApplicationError(Exception):
    """Base class for errors safe to translate at an application boundary."""

    status_code = 500
    public_detail = "内部服务器错误"
    error_code = "application_error"


class AuthenticationUnavailable(ApplicationError):
    """The credential store could not be reached or evaluated reliably."""

    status_code = 503
    public_detail = "认证服务暂时不可用"
    error_code = "authentication_unavailable"


class UpstreamServiceError(ApplicationError):
    """An external model or service failed while handling the request."""

    status_code = 502
    public_detail = "上游服务调用失败"
    error_code = "upstream_service_error"


class StorageUnavailable(ApplicationError):
    """A required database or storage backend is unavailable."""

    status_code = 503
    public_detail = "存储服务暂时不可用"
    error_code = "storage_unavailable"
