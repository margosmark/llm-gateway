"""HTTP layer for the gateway.

Standard library only. Provides a tiny request/response model, a path router,
and a threaded HTTP server that supports buffered JSON responses and
incremental server-sent-event streaming.
"""

from gateway.http.core import (
    Headers,
    Request,
    Response,
    Router,
    Server,
    StreamResponse,
    json_response,
    no_content,
)

__all__ = [
    "Headers",
    "Request",
    "Response",
    "Router",
    "Server",
    "StreamResponse",
    "json_response",
    "no_content",
]
