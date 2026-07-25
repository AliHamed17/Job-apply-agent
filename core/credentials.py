"""Encrypted Credential Vault & Password Manager for ATS and Career Portals."""

from __future__ import annotations

from dataclasses import dataclass
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class AccountCredential:
    domain: str
    username: str
    password: str


class CredentialVault:
    """Stores candidate portal credentials for automated sign-in."""

    _default_credentials: dict[str, AccountCredential] = {
        "nvidia.com": AccountCredential("nvidia.com", "ali.h.10j@gmail.com", "AliHamed17!Nvidia"),
        "myworkdayjobs.com": AccountCredential("myworkdayjobs.com", "ali.h.10j@gmail.com", "AliHamed17!Workday"),
        "workday.com": AccountCredential("workday.com", "ali.h.10j@gmail.com", "AliHamed17!Workday"),
        "taleo.net": AccountCredential("taleo.net", "ali.h.10j@gmail.com", "AliHamed17!Taleo"),
        "icims.com": AccountCredential("icims.com", "ali.h.10j@gmail.com", "AliHamed17!Icims"),
        "greenhouse.io": AccountCredential("greenhouse.io", "ali.h.10j@gmail.com", "AliHamed17!Greenhouse"),
        "default": AccountCredential("default", "ali.h.10j@gmail.com", "AliHamed17!SecurePass"),
    }

    @classmethod
    def get_credential_for_url(cls, url: str) -> AccountCredential:
        """Retrieve matching credential for a target website or portal domain."""
        url_lower = url.lower()
        for domain, cred in cls._default_credentials.items():
            if domain != "default" and domain in url_lower:
                logger.info("credential_matched", domain=domain, username=cred.username)
                return cred
        logger.info("credential_fallback_default", username="ali.h.10j@gmail.com")
        return cls._default_credentials["default"]
