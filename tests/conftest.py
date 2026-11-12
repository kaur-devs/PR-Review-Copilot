"""Shared test setup.

Runs before any test file is imported, so the environment is already pointing
at the test database by the time the application code loads.
"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# A fixed secret for tests. Never a real one.
TEST_SECRET = "test-secret-do-not-use-in-production"

# Read the real .env first, because it holds the test database URL.
load_dotenv(PROJECT_ROOT / ".env")

# Then override everything the tests should not use real values for. The test
# database is a completely separate database, so a test run can never touch
# development data.
# The suite drops and recreates every table, then truncates between tests.
# Pointed at the development database that is one command away from deleting
# the real review history, so it refuses rather than trusting the .env.
if os.environ.get("TEST_DATABASE_URL") == os.environ.get("DATABASE_URL"):
    raise RuntimeError(
        "TEST_DATABASE_URL and DATABASE_URL are the same database. "
        "The tests would erase it. Point TEST_DATABASE_URL somewhere separate."
    )

os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["GITHUB_WEBHOOK_SECRET"] = TEST_SECRET
os.environ["GITHUB_APP_ID"] = "000000"
os.environ["GITHUB_CLIENT_ID"] = "Iv1.testclientid"

# A model that does not exist, so a test can never reach a real provider.
os.environ["LLM_API_KEY"] = "test-llm-key"
os.environ["LLM_BASE_URL"] = "https://model.invalid/v1"
os.environ["LLM_MODEL"] = "test-model"

# The development trace would drown the test output.
os.environ["PIPELINE_TRACE"] = "0"

# Startup recovery is tested on purpose, in tests/test_recovery.py. Leaving it
# on would mean every test that starts the app spawns a background scan.
os.environ["RECOVERY_ON_STARTUP"] = "0"

# The agentic pipeline is tested on purpose, in tests/test_graph.py, with a
# model that answers the specialist prompts. Most tests here exercise the
# single-pass pipeline and would otherwise take a path their fake model
# cannot answer, failing for a reason that has nothing to do with them.
os.environ["AGENTIC_PIPELINE"] = "0"

# A throwaway private key, generated fresh each run.
#
# It has to be a genuine RSA key rather than a placeholder, because the
# authentication code really signs with it. It is never GitHub's key and is
# thrown away when the tests finish.
def _make_test_key() -> Path:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = PROJECT_ROOT / "tests" / "fake_key.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


os.environ["GITHUB_APP_PRIVATE_KEY_PATH"] = str(_make_test_key())


def run_on_database(work):
    """Run one piece of database work and give back the result.

    Tests are ordinary functions, but the database code is asynchronous, so
    each call here opens a connection, does the work, and closes it again.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    async def go():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with engine.begin() as connection:
                return await work(connection)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def needs_database(request) -> bool:
    """True for a test under tests/database/.

    The folder is the marker. Making it autouse for everything meant no
    test could run without Postgres, including the ones that touch nothing
    but a pure function.
    """
    return "tests/database/" in request.path.as_posix()


@pytest.fixture(scope="session")
def database_schema():
    """Build the tables once, for the tests that need them.

    Against the test database, which is separate from the development one,
    so a run can never delete real data.
    """
    from src.db.models import Base

    async def rebuild(connection):
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    run_on_database(rebuild)
    yield


@pytest.fixture(autouse=True)
def empty_tables(request):
    """Empty every table before each database test.

    Each one starts from nothing and row ids start at 1 again, which keeps
    the assertions readable.
    """
    if not needs_database(request):
        yield
        return

    from sqlalchemy import text

    from src.db.models import Base

    request.getfixturevalue("database_schema")

    table_names = ", ".join(Base.metadata.tables)

    async def truncate(connection):
        await connection.execute(
            text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE")
        )

    run_on_database(truncate)
    yield


@pytest.fixture(autouse=True)
def forget_rate_limits():
    """No test inherits another's token budget.

    The limiter remembers what a provider last said, per model, for the
    life of the process. A budget left over from one test makes the next
    one wait for a window that never opens, which looks like a hang.
    """
    from src.llm.provider import forget_rate_limits as forget

    forget()
    yield
    forget()


@pytest.fixture
def recorded_pipeline_calls(monkeypatch):
    calls = []

    async def record(event, review_id, **kwargs):
        calls.append((event, review_id))

    monkeypatch.setattr("src.webhooks.router.process_pull_request", record)
    return calls


@pytest.fixture
def client(database_schema, recorded_pipeline_calls):
    from fastapi.testclient import TestClient

    from src.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def no_backoff_waiting(monkeypatch):
    """Retry backoff is real seconds. Tests should not sit through it."""
    from src.llm import budget as provider

    async def instant(seconds):
        return None

    monkeypatch.setattr(provider.asyncio, "sleep", instant)
