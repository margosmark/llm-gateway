"""Identifier grammar and validation.

Regression cover for **R4**: the configuration accepted an empty or
arbitrary alias and profile identifier, and accepted a route that pointed
at an unknown provider or model. The result was a published alias that
appeared in ``/v1/models`` and then failed during routing, and a profile
that could not be given a token because the credential store rejected the
identifier afterwards.

One grammar, one length limit, checked **before** anything is persisted.
"""

from __future__ import annotations

import re
from typing import Any

#: An identifier is a short, printable, filesystem-safe token. It must start
#: with a letter or digit, so it can never be a path segment of its own.
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

MAX_IDENTIFIER_LENGTH = 64

#: Model ids are provider-qualified and may carry a wider character set,
#: but are still bounded and free of control characters.
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")

MAX_MODEL_ID_LENGTH = 192


class ValidationError(ValueError):
    """A value the owner supplied cannot be accepted.

    The message is written for the owner and names the field and the rule.
    """

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


def validate_identifier(value: Any, kind: str) -> str:
    """Validate an alias name, a profile id, or another identifier.

    *kind* names the field in the message, so the owner knows which value
    was refused.
    """
    if not isinstance(value, str):
        raise ValidationError(f"the {kind} must be a string", kind)
    candidate = value.strip()
    if not candidate:
        raise ValidationError(f"the {kind} must not be empty", kind)
    if len(candidate) > MAX_IDENTIFIER_LENGTH:
        raise ValidationError(
            f"the {kind} must be at most {MAX_IDENTIFIER_LENGTH} characters", kind
        )
    if not IDENTIFIER_PATTERN.match(candidate):
        raise ValidationError(
            f"the {kind} may contain only letters, digits, dots, underscores and "
            "hyphens, and must start with a letter or a digit",
            kind,
        )
    return candidate


def validate_model_id(value: Any) -> str:
    """Validate a model id."""
    if not isinstance(value, str):
        raise ValidationError("the model id must be a string", "model_id")
    candidate = value.strip()
    if not candidate:
        raise ValidationError("the model id must not be empty", "model_id")
    if len(candidate) > MAX_MODEL_ID_LENGTH:
        raise ValidationError(
            f"the model id must be at most {MAX_MODEL_ID_LENGTH} characters", "model_id"
        )
    if not MODEL_ID_PATTERN.match(candidate):
        raise ValidationError(
            "the model id may contain only letters, digits, dots, underscores, "
            "hyphens, colons and slashes, and must start with a letter or a digit",
            "model_id",
        )
    return candidate


def validate_alias_name(value: Any) -> str:
    return validate_identifier(value, "alias name")


def validate_profile_id(value: Any) -> str:
    return validate_identifier(value, "profile id")
