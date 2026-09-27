"""Opening connections to the database.

An "engine" is the thing that knows how to reach the database. A "session" is
one conversation with it: you make some changes, then commit them.

We build the engine once and reuse it, because setting one up is expensive.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.config import get_settings
from src.db.url import normalise_database_url


@lru_cache(maxsize=1)
def get_engine():
    """The one engine for this process. lru_cache means it is built once."""
    url, connect_args = normalise_database_url(get_settings().database_url)

    # NullPool means "do not keep connections open between uses".
    #
    # Normally keeping them open is faster. Here it is the wrong choice for
    # two reasons: our cloud database already pools connections on its side,
    # so doing it again adds nothing; and a kept-open connection belongs to
    # the piece of the program that opened it, which causes confusing failures
    # when something else picks it up later.
    return create_async_engine(url, connect_args=connect_args, poolclass=NullPool)


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Makes new sessions on demand."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Gives one session to one web request, then closes it.

    FastAPI calls this for us when an endpoint asks for a session.
    """
    async with get_session_factory()() as session:
        yield session
