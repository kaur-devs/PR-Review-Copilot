"""Settings for the whole service.

Everything secret or environment-specific lives in a .env file, which is
never committed. This module reads that file once and hands the values out.

We check everything is present the moment the service starts. The alternative
is discovering a missing secret when the first pull request arrives, which is
far worse.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Values already in the real environment win, which is how deployment overrides.
load_dotenv()

REQUIRED_VARS = (
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_APP_ID",
    "GITHUB_CLIENT_ID",
    "DATABASE_URL",
)

# The App's private key arrives one of two ways and exactly one is needed, so
# it cannot go in REQUIRED_VARS, which demands every name it lists.
#
# A laptop points at the .pem file it downloaded from GitHub. A container has
# no such file, so a deployment passes the key's PEM text instead. Env wins
# over file, consistent with load_dotenv above: setting the text overrides a
# path left behind in .env.
KEY_TEXT_VAR = "GITHUB_APP_PRIVATE_KEY"
KEY_PATH_VAR = "GITHUB_APP_PRIVATE_KEY_PATH"


@dataclass(frozen=True)
class Settings:
    """All our settings in one place.

    frozen=True means nothing can change these after startup, so no part of
    the code can quietly rewrite a secret.
    """

    webhook_secret: str

    app_id: str

    # The issuer claim when we sign a token proving we are the App.
    client_id: str

    database_url: str

    # Exactly one of these is set; get_settings guarantees it. Holding the
    # text keeps the key off the container's disk — it does not keep it out
    # of memory, because a variable we were given through the environment is
    # in os.environ for the life of the process either way.
    private_key: str | None = None
    private_key_path: Path | None = None

    def read_private_key(self) -> str:
        """The private key, whichever way it was supplied.

        The file is read on demand rather than held, so on a laptop the key
        is in memory only for as long as signing takes.
        """
        if self.private_key is not None:
            return self.private_key
        if self.private_key_path is not None:
            return self.private_key_path.read_text()
        raise RuntimeError(
            f"No private key configured: set {KEY_TEXT_VAR} or {KEY_PATH_VAR}."
        )


def _check_is_a_usable_key(pem: str, source: str) -> None:
    """Parse the key now so a bad one fails at startup, not mid-review.

    Checking the variable is merely non-empty is not enough. A PEM truncated
    by a bad paste into a hosting provider's form passes that check and then
    raises inside pyjwt on the first webhook — exactly the late failure this
    module exists to prevent. Parsing costs a few milliseconds once.

    The parsed key is deliberately thrown away; signing re-reads it.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    try:
        load_pem_private_key(pem.encode(), password=None)
    except Exception as exc:
        raise RuntimeError(
            f"The private key from {source} is not a readable PEM key: {exc}. "
            "It must keep its -----BEGIN-----/-----END----- lines and its "
            "internal line breaks; a missing trailing newline is fine. A key "
            "pasted into a hosting provider's form is the usual thing to check."
        ) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build the settings, or explain clearly what is missing.

    lru_cache means the work happens once. Every later call gets the same
    object back.
    """
    missing = [name for name in REQUIRED_VARS if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            f"Missing environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in."
        )

    # Empty is absent: a hosting provider will happily save a blank variable.
    key_text = os.getenv(KEY_TEXT_VAR) or None
    raw_path = os.getenv(KEY_PATH_VAR) or None

    if key_text is None and raw_path is None:
        raise RuntimeError(
            f"Missing environment variables: one of {KEY_TEXT_VAR} (the key's "
            f"PEM text, how a deployment supplies it) or {KEY_PATH_VAR} (a "
            "file, how a laptop does). Copy .env.example to .env and fill it in."
        )

    key_path: Path | None = None

    if key_text is not None:
        _check_is_a_usable_key(key_text, KEY_TEXT_VAR)
    else:
        key_path = Path(raw_path).expanduser()
        if not key_path.is_file():
            raise RuntimeError(
                f"Private key not found at {key_path}. Generate one on the "
                f"GitHub App settings page and point {KEY_PATH_VAR} at it, "
                f"or pass the key's text as {KEY_TEXT_VAR}."
            )
        _check_is_a_usable_key(key_path.read_text(), str(key_path))

    return Settings(
        webhook_secret=os.environ["GITHUB_WEBHOOK_SECRET"],
        app_id=os.environ["GITHUB_APP_ID"],
        client_id=os.environ["GITHUB_CLIENT_ID"],
        database_url=os.environ["DATABASE_URL"],
        private_key=key_text,
        private_key_path=key_path,
    )
