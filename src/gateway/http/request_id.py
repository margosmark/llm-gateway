"""Validation for client-supplied request correlation identifiers."""

from __future__ import annotations

import re


MAX_CLIENT_REQUEST_ID_LENGTH = 128
_CLIENT_REQUEST_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._:-]+\Z", re.ASCII)


def validate_client_request_id(value: str) -> str:
    """Return a safe correlation id, refusing blank, long or non-token values."""
    if not isinstance(value, str) or not value or len(value) > MAX_CLIENT_REQUEST_ID_LENGTH:
        raise ValueError("invalid X-Request-Id")
    if _CLIENT_REQUEST_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid X-Request-Id")
    return value
