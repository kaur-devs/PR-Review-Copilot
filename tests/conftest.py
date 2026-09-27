"""Shared test setup.

Runs before any test file is imported, so the environment is already pointing
at the test database by the time the application code loads.
"""

import os
from pathlib import Path

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

# A fake key file, so the startup check passes without the real one.
FAKE_KEY = PROJECT_ROOT / "tests" / "fake_key.pem"
FAKE_KEY.write_text("-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n")
os.environ["GITHUB_APP_PRIVATE_KEY_PATH"] = str(FAKE_KEY)
