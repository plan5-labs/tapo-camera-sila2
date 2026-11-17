"""Camera credential storage.

Credentials live in the OS credential store (Windows Credential Manager / macOS Keychain /
Secret Service) via ``keyring`` -- never in the repository. Environment variables
``TAPO_HOST`` / ``TAPO_USER`` / ``TAPO_PASSWORD`` override the stored values
(useful for CI or containers).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import keyring

SERVICE = "tapo-camera-controller"
_FIELDS = ("host", "user", "password")


@dataclass(frozen=True)
class Credentials:
    host: str
    user: str
    password: str

    def __repr__(self) -> str:  # never leak the password through logs / tracebacks
        return f"Credentials(host={self.host!r}, user={self.user!r}, password='***')"

    __str__ = __repr__

    def redact(self, text: str) -> str:
        """Mask the password (raw and URL-encoded) in arbitrary text."""
        from urllib.parse import quote

        for s in {self.password, quote(self.password, safe="")}:
            if s:
                text = text.replace(s, "***")
        return text


def _key(profile: str, field: str) -> str:
    return f"{profile}:{field}"


def load(profile: str = "default") -> Credentials:
    values = {}
    for f in _FIELDS:
        v = os.environ.get(f"TAPO_{f.upper()}") or keyring.get_password(SERVICE, _key(profile, f))
        if not v:
            raise LookupError(
                f"camera {f} not found: run `uv run scripts/set_credentials.py` "
                f"or set TAPO_{f.upper()}"
            )
        values[f] = v
    return Credentials(**values)


def save(creds: Credentials, profile: str = "default") -> None:
    for f in _FIELDS:
        keyring.set_password(SERVICE, _key(profile, f), getattr(creds, f))


def delete(profile: str = "default") -> None:
    for f in _FIELDS:
        try:
            keyring.delete_password(SERVICE, _key(profile, f))
        except keyring.errors.PasswordDeleteError:
            pass
