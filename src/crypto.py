"""Symmetric encryption for OAuth tokens at rest.

Tokens are the one thing in this database that grants control of a real account,
so they are never stored in plaintext and never written to logs.
"""

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_PREFIX = "enc:v1:"


def _cipher() -> Fernet:
    key = get_settings().app_encryption_key
    if not key:
        raise RuntimeError(
            "APP_ENCRYPTION_KEY is not set. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str | None) -> str | None:
    if plaintext is None:
        return None
    return _PREFIX + _cipher().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str | None) -> str | None:
    """Accepts values written before encryption was enabled, so an existing
    deployment keeps working while tokens are migrated on next refresh."""
    if ciphertext is None:
        return None
    if not ciphertext.startswith(_PREFIX):
        return ciphertext
    try:
        return _cipher().decrypt(ciphertext[len(_PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Stored token could not be decrypted - APP_ENCRYPTION_KEY changed?"
        ) from exc
