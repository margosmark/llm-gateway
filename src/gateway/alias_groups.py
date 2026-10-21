"""Convenience operations over tagged aliases; routing remains per alias."""

from __future__ import annotations

from gateway.config import GatewayConfig
from gateway.identifiers import ValidationError, validate_identifier


def set_group_enabled(config: GatewayConfig, tag: object, enabled: object) -> list[str]:
    """Set publication for every alias with *tag*, without changing its policy."""
    normalized_tag = validate_identifier(tag, "alias tag")
    if not isinstance(enabled, bool):
        raise ValidationError("enabled must be a boolean", "enabled")
    aliases = sorted((alias for alias in config.aliases.values() if alias.tag == normalized_tag), key=lambda a: a.name)
    if not aliases:
        raise ValidationError("no aliases have that tag", "tag")
    if enabled and any(alias.primary is None for alias in aliases):
        raise ValidationError("every alias in the group needs a primary route before publishing", "tag")
    for alias in aliases:
        alias.enabled = enabled
    return [alias.name for alias in aliases]
