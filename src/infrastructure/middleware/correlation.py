# src/infrastructure/middleware/correlation.py
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Stores the current request ID for the duration of a single request.
# Other modules (logger, error handler) read this via get_request_id().
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """Returns the correlation ID for the current request, or '' if outside a request."""
    return _request_id_var.get()


def set_request_id(request_id: str) -> None:
    """Set the correlation ID for the current context. Used by workers processing queue messages."""
    _request_id_var.set(request_id)


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """
    Assigns a unique ID to every incoming request.

    Reads the X-Request-ID header if provided by the caller (useful for
    end-to-end tracing across microservices). Generates req_<8 hex chars>
    otherwise.

    The ID is:
    - stored in request.state.request_id
    - accessible via get_request_id() throughout the request lifecycle
    - echoed back in the X-Request-ID response header
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        req_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:8]}"
        token = _request_id_var.set(req_id)
        request.state.request_id = req_id
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = req_id
            return response
        finally:
            _request_id_var.reset(token)
