"""Proving a webhook came from GitHub.

The endpoint is public, so every message is checked before we act on it.
GitHub signs the body with a shared secret; we sign it again and compare.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="


def expected_signature(raw_body: bytes, secret: str) -> str:
    fingerprint = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + fingerprint


def is_valid_signature(raw_body: bytes, header_value: str | None, secret: str) -> bool:
    """True if this message was signed with our secret.

    raw_body must be the bytes GitHub sent: re-encoding the parsed JSON
    changes the spacing and the signature no longer matches.
    """
    if not header_value:
        return False

    # compare_digest, not ==: timing must not reveal a partial match.
    return hmac.compare_digest(expected_signature(raw_body, secret), header_value)
