"""Structured API errors.

Every domain failure is raised as :class:`ApiError` and rendered as::

    {"error": {"code": "...", "message": "...", "details": {...}}}

FastAPI validation errors are rendered in the same envelope by the handler in
``app.main`` so clients only have to understand one error shape.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """A business-rule failure with an HTTP status and a stable error code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return {"error": body}
