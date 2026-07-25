"""Compatibility guard for the removed plaintext portal credential vault.

Employer automation uses dedicated persistent browser sessions. Passwords are
entered only by the operator in the employer's own page during one-time
bootstrap; this project never extracts or stores them.
"""

from __future__ import annotations


class CredentialAccessDisabledError(RuntimeError):
    """Raised when legacy code asks the application for a portal password."""


class CredentialVault:
    """Deprecated fail-closed shim; no credentials are retained."""

    @classmethod
    def get_credential_for_url(cls, _url: str):
        raise CredentialAccessDisabledError(
            "PASSWORD_AUTOFILL_DISABLED: bootstrap a dedicated portal session instead."
        )
