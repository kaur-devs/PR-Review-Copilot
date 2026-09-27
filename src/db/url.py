"""Fixing up a database connection string so our driver understands it.

Hosted database providers such as Neon give you a connection string like:

    postgresql://user:pass@host/dbname?sslmode=require&channel_binding=require

That is written for a different Postgres library than the one we use. Ours is
called asyncpg, and it does not understand "sslmode" or "channel_binding". If
you paste the string in unchanged, the connection fails with a confusing error
about unexpected arguments.

Rather than making every person who deploys this remember to hand-edit the
string, we fix it here once.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Settings other Postgres libraries understand but asyncpg does not.
SETTINGS_ASYNCPG_DOES_NOT_KNOW = {"sslmode", "channel_binding"}


def normalise_database_url(url: str) -> tuple[str, dict[str, object]]:
    """Return a usable connection string plus any extra connection settings.

    Three fixes happen here:

    1. "postgresql://" tells SQLAlchemy to use the old, blocking driver. We
       change it to "postgresql+asyncpg://" so it uses the async one.

    2. "sslmode=require" is removed from the string and passed separately as
       a setting asyncpg does understand.

    3. If the host name contains "-pooler", we are connecting through a
       connection pooler. A pooler can send our next query to a different
       server, so anything the driver remembered about the last one is no
       longer valid. Turning off its memory of prepared queries avoids that.
    """
    parts = urlsplit(url)
    settings = dict(parse_qsl(parts.query))
    connect_args: dict[str, object] = {}

    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"

    if settings.get("sslmode"):
        connect_args["ssl"] = "require"

    if "-pooler" in (parts.hostname or ""):
        connect_args["statement_cache_size"] = 0

    kept = {k: v for k, v in settings.items() if k not in SETTINGS_ASYNCPG_DOES_NOT_KNOW}

    fixed_url = urlunsplit(
        (scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )
    return fixed_url, connect_args
