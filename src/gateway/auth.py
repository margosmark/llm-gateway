"""Client-profile authentication for anonymous or token-authenticated APIs."""

from __future__ import annotations

import time

from gateway.config import ConfigStore, Profile
from gateway.credentials import CredentialStore, hash_caller_token, new_caller_token

#: The identifier of the single anonymous profile used in LAN no-auth mode.
ANONYMOUS_PROFILE_ID = "anonymous-lan"

class ProfileAuth:
    """Resolves an API request to a client profile."""

    def __init__(self, config: ConfigStore, credentials: CredentialStore) -> None:
        self._config = config
        self._credentials = credentials

    def anonymous_profile(self) -> Profile:
        """The single profile used by every caller in LAN no-auth mode."""
        config = self._config.snapshot()
        return Profile(
            profile_id=ANONYMOUS_PROFILE_ID,
            name="Anonymous LAN profile",
            enabled=True,
            # LAN API invocation is anonymous by design, so this profile is
            # explicitly granted every published alias.
            all_aliases=True,
            allowed_aliases=[],
            anonymous=True,
        )

    def resolve(self, bearer_token: str | None) -> tuple[Profile | None, str]:
        """Resolve a bearer token to a profile.

        Returns ``(profile, reason)``. In LAN no-auth mode the anonymous
        profile is returned regardless of the token.
        """
        # A reachable LAN peer is a caller by product decision. Profiles and
        # caller tokens remain stored as configuration/future attribution data;
        # they do not gate inference on this trusted-LAN appliance.
        return self.anonymous_profile(), "trusted_lan"

    def issue_token(self, profile: Profile) -> str:
        """Mint a new caller token for a profile and store its hash."""
        token = new_caller_token()
        self._credentials.handle(f"profile_{profile.profile_id}").write(
            {"token_hash": hash_caller_token(token), "issued_at": time.time()}
        )
        return token

    def revoke_token(self, profile_id: str) -> bool:
        return self._credentials.handle(f"profile_{profile_id}").clear()

    def profile_has_token(self, profile_id: str) -> bool:
        payload = self._credentials.handle(f"profile_{profile_id}").read()
        return bool(payload and payload.get("token_hash"))

    def set_profile_has_token(self, profile_id: str, has_token: bool) -> None:
        """Mirror the token-presence flag into the configuration.

        The token *hash* never enters the configuration; only the fact that a
        token exists does.
        """
        with self._config.mutate() as config:
            profile = config.profiles.get(profile_id)
            if profile is not None:
                profile.has_token = has_token


def profile_allows(profile: Profile, alias_name: str) -> bool:
    """Whether a profile may use an alias.

    A profile **denies by default**. It may use an alias it was explicitly
    granted, or any published alias when the owner granted access to all of
    them with the separate, explicit flag. An empty allow-list is not a
    grant.
    """
    if profile.all_aliases:
        return True
    return alias_name in profile.allowed_aliases


def access_mode_description(mode: str) -> str:
    return (
        "Trusted LAN admin mode. API calls are anonymous, and any device that can reach this address can "
        "configure and use this gateway. Profiles and caller tokens do not restrict LAN access."
    )
