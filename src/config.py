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

# Reads .env into the environment. Values already set in the real environment
# win, which is how deployment overrides local development.
load_dotenv()

# The service cannot start without these.
REQUIRED_VARS = (
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_APP_ID",
    "GITHUB_CLIENT_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "DATABASE_URL",
)


@dataclass(frozen=True)
class Settings:
    """All our settings in one place.

    frozen=True means nothing can change these after startup, so no part of
    the code can quietly rewrite a secret.
    """

    # Shared password between us and GitHub, used to prove a webhook is real.
    webhook_secret: str

    # Numeric id of our GitHub App.
    app_id: str

    # Used when we sign a token to prove we are the App.
    client_id: str

    # Where the App's private key file sits on disk.
    private_key_path: Path

    # How to reach PostgreSQL.
    database_url: str

    def read_private_key(self) -> str:
        """The contents of the private key file.

        Read on demand rather than at startup, so the key is not sitting in
        memory any longer than it needs to be.
        """
        return self.private_key_path.read_text()


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

    key_path = Path(os.environ["GITHUB_APP_PRIVATE_KEY_PATH"]).expanduser()
    if not key_path.is_file():
        raise RuntimeError(
            f"Private key not found at {key_path}. Generate one on the "
            "GitHub App settings page and point GITHUB_APP_PRIVATE_KEY_PATH at it."
        )

    return Settings(
        webhook_secret=os.environ["GITHUB_WEBHOOK_SECRET"],
        app_id=os.environ["GITHUB_APP_ID"],
        client_id=os.environ["GITHUB_CLIENT_ID"],
        private_key_path=key_path,
        database_url=os.environ["DATABASE_URL"],
    )
