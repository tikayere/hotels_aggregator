"""Application-layer encryption for hotel_credential secrets (contract 4.2).

api_key / webhook_secret are stored as Fernet ciphertext, never plaintext,
never logged. The Fernet key comes from CREDENTIAL_ENCRYPTION_KEY. If that is
unset (empty in .env.example) a per-process ephemeral key is generated with a
loud warning so local dev still boots -- ciphertext written under an ephemeral
key will not decrypt across restarts, which is acceptable for dev only.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet

from app.config import settings

logger = logging.getLogger("aggregator.crypto")

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet

    key = settings.credential_encryption_key
    if not key:
        generated = Fernet.generate_key()
        logger.warning(
            "CREDENTIAL_ENCRYPTION_KEY is unset -- using an EPHEMERAL per-process "
            "key. Stored hotel credentials will not survive a restart. Set a real "
            "key before any non-dev use."
        )
        _fernet = Fernet(generated)
    else:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a credential for at-rest storage. Returns urlsafe ciphertext."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored credential back to plaintext for use in an outbound call."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def generate_encryption_key() -> str:
    """Helper to mint a valid CREDENTIAL_ENCRYPTION_KEY for an operator."""
    return Fernet.generate_key().decode()
