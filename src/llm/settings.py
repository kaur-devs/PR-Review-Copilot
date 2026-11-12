"""Where the model lives, and how hard we try to reach it."""

from __future__ import annotations

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"

REQUEST_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 4

# Used only when the provider does not tell us how long to wait.
BACKOFF_SECONDS = (5.0, 20.0, 45.0)
MAX_WAIT_SECONDS = 90.0

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

DAILY_LIMIT_MARKER = "tokens per day"
