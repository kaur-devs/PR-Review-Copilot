"""Connection strings from a hosted provider need fixing before use."""

from src.db.url import normalise_database_url

# What Neon gives you when you choose the direct connection.
NEON_DIRECT = (
    "postgresql://owner:secret@ep-cool-frost-12345.eu-central-1.aws.neon.tech"
    "/neondb?sslmode=require&channel_binding=require"
)

# What Neon gives you when you choose the pooled connection. Note "-pooler".
NEON_POOLED = (
    "postgresql://owner:secret@ep-cool-frost-12345-pooler.eu-central-1.aws.neon.tech"
    "/neondb?sslmode=require&channel_binding=require"
)

# What we use on our own machine. Already correct.
LOCAL = "postgresql+asyncpg://prcopilot:pw@localhost:5432/pr_review_copilot"


def test_the_async_driver_is_selected():
    url, _ = normalise_database_url(NEON_DIRECT)
    assert url.startswith("postgresql+asyncpg://")


def test_sslmode_is_moved_out_of_the_url():
    """Our driver rejects it inside the string but accepts it separately."""
    url, connect_args = normalise_database_url(NEON_DIRECT)
    assert "sslmode" not in url
    assert connect_args["ssl"] == "require"


def test_channel_binding_is_removed():
    url, _ = normalise_database_url(NEON_DIRECT)
    assert "channel_binding" not in url


def test_the_username_password_and_database_are_kept():
    url, _ = normalise_database_url(NEON_DIRECT)
    assert "owner:secret@" in url
    assert url.endswith("/neondb")


def test_a_pooled_connection_turns_off_remembered_queries():
    _, connect_args = normalise_database_url(NEON_POOLED)
    assert connect_args["statement_cache_size"] == 0


def test_a_direct_connection_keeps_remembered_queries():
    _, connect_args = normalise_database_url(NEON_DIRECT)
    assert "statement_cache_size" not in connect_args


def test_our_local_url_is_left_alone():
    url, connect_args = normalise_database_url(LOCAL)
    assert url == LOCAL
    assert connect_args == {}


def test_the_older_postgres_scheme_is_also_handled():
    """Some providers still hand out "postgres://" rather than "postgresql://"."""
    url, _ = normalise_database_url("postgres://u:p@host:5432/db")
    assert url.startswith("postgresql+asyncpg://")
