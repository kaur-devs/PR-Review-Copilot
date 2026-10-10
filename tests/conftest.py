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


@pytest.fixture(scope="session", autouse=True)
def database_schema():
    """Build the tables once, before any test runs.

    This uses the test database, which is a completely separate database from
    the one used while developing, so a test run can never delete real data.
    """
    from src.db.models import Base

    async def rebuild(connection):
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    run_on_database(rebuild)
    yield


@pytest.fixture(autouse=True)
def empty_tables(database_schema):
    """Empty every table before each test.

    Each test then starts from nothing, and row numbering starts at 1 again,
    which keeps the tests easy to read.
    """
    from sqlalchemy import text

    from src.db.models import Base

    table_names = ", ".join(Base.metadata.tables)

    async def truncate(connection):
        await connection.execute(
            text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE")
        )

    run_on_database(truncate)
    yield


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
    from src.review import provider

    async def instant(seconds):
        return None

    monkeypatch.setattr(provider.asyncio, "sleep", instant)
