"""Checking that a webhook really came from GitHub.

Our webhook address is public. Anyone who finds it can send us anything, so
every message has to be proven genuine before we act on it.

How it works: we and GitHub share a secret string. GitHub mixes that secret
with the exact bytes of the message to produce a fingerprint, and sends the
fingerprint in a header. We do the same calculation. If our fingerprint
matches theirs, only someone holding the secret could have sent it.

The technique is called HMAC. "SHA256" is the mixing function.
"""

from __future__ import annotations

import hashlib
import hmac

# The header GitHub puts the fingerprint in.
SIGNATURE_HEADER = "X-Hub-Signature-256"

# GitHub writes the fingerprint as "sha256=<hex digits>".
SIGNATURE_PREFIX = "sha256="


def expected_signature(raw_body: bytes, secret: str) -> str:
    """Work out what the fingerprint should be for this message."""
    fingerprint = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + fingerprint


def is_valid_signature(raw_body: bytes, header_value: str | None, secret: str) -> bool:
    """True if this message was really signed with our secret.

    Two details matter here.

    First, raw_body must be the exact bytes GitHub sent. If you read the
    message as JSON and then turn it back into text, the spacing and the order
    of the keys change. The meaning is identical but the bytes are not, so the
    fingerprint no longer matches.

    Second, we compare with compare_digest instead of "==". A normal comparison
    stops as soon as it finds a difference, so it takes slightly longer when
    more of the guess was right. Someone measuring those tiny differences could
    work out the correct answer one character at a time. compare_digest always
    takes the same amount of time.
    """
    if not header_value:
        return False
    return hmac.compare_digest(expected_signature(raw_body, secret), header_value)
