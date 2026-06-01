"""HTTP middleware: per-request correlation id + structured access logging.

Binds a request id (read from the configured header or generated) onto the
structured-logging context so every log line emitted while handling the request
— including those from the workflow runtime and service layers — carries it.
The id is echoed back on the response header for end-to-end tracing.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import bind_log_context, clear_log_context, get_logger
from app.shared.workflow.utils import new_id


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a correlation id + access log to every request."""

    def __init__(self, app: object, *, service: str, header: str) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._service = service
        self._header = header
        self._log = get_logger("http")

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get(self._header) or new_id("req")
        clear_log_context()
        bind_log_context(
            request_id=request_id,
            service=self._service,
            method=request.method,
            path=request.url.path,
        )
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self._log.exception("http.request.unhandled")
            clear_log_context()
            raise
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        self._log.info(
            "http.request", status=response.status_code, duration_ms=duration_ms
        )
        response.headers[self._header] = request_id
        clear_log_context()
        return response
