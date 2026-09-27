"""The signature check is the only thing standing between our service and
anyone who finds the webhook address, so it gets tested thoroughly."""

import hashlib
import hmac
import json

from src.webhooks.signature import expected_signature, is_valid_signature

SECRET = "our-shared-secret"
BODY = b'{"action":"opened","number":7}'


def sign(body: bytes, secret: str = SECRET) -> str:
    """Produce the fingerprint the way GitHub would."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_a_correctly_signed_message_is_accepted():
    assert is_valid_signature(BODY, sign(BODY), SECRET)


def test_a_message_changed_after_signing_is_rejected():
    """Someone intercepted it and edited the body."""
    assert not is_valid_signature(b'{"action":"closed"}', sign(BODY), SECRET)


def test_a_message_signed_with_the_wrong_secret_is_rejected():
    """Someone is guessing at our secret."""
    assert not is_valid_signature(BODY, sign(BODY, "wrong-secret"), SECRET)


def test_a_message_with_no_signature_is_rejected():
    assert not is_valid_signature(BODY, None, SECRET)
    assert not is_valid_signature(BODY, "", SECRET)


def test_a_fingerprint_without_the_sha256_prefix_is_rejected():
    """Right fingerprint, wrong format. Still no."""
    bare = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert not is_valid_signature(BODY, bare, SECRET)


def test_the_fingerprint_has_the_prefix_github_uses():
    assert expected_signature(BODY, SECRET).startswith("sha256=")


def test_rebuilding_the_json_breaks_the_fingerprint():
    """This is why the endpoint must use the raw bytes.

    Reading the message as JSON and writing it back out gives text that means
    exactly the same thing, but the spacing differs, so the fingerprint no
    longer matches. Getting this wrong is the classic mistake here.
    """
    signature = sign(BODY)
    rebuilt = json.dumps(json.loads(BODY)).encode()

    assert rebuilt != BODY
    assert not is_valid_signature(rebuilt, signature, SECRET)
