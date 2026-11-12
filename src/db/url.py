"""Making a hosted provider's connection string usable by asyncpg."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Neon and friends put these in the URL; asyncpg rejects them as unexpected
# arguments, with an error that does not say which one is to blame.
SETTINGS_ASYNCPG_DOES_NOT_KNOW = {"sslmode", "channel_binding"}


def normalise_database_url(url: str) -> tuple[str, dict[str, object]]:
    """Return a connection string for the async driver, plus its settings."""
    parts = urlsplit(url)
    settings = dict(parse_qsl(parts.query))
    connect_args: dict[str, object] = {}

    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"

    if settings.get("sslmode"):
        connect_args["ssl"] = "require"

    # Through a pooler the next query may land on a different server, so
    # anything the driver cached about the last one is wrong.
    if "-pooler" in (parts.hostname or ""):
        connect_args["statement_cache_size"] = 0

    kept = {k: v for k, v in settings.items() if k not in SETTINGS_ASYNCPG_DOES_NOT_KNOW}

    fixed_url = urlunsplit(
        (scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )
    return fixed_url, connect_args
