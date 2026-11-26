"""The access log should lose /health, and keep everything else.

A keep-alive pinger asks for /health every five minutes. Those lines are of
no interest and they push the lines that matter out of a host's short log
retention. Suppressing them is only safe if it is narrow, which is what
these tests pin down.
"""

import logging

from src.main import HealthCheckFilter


def _access_record(path: str, status: int = 200) -> logging.LogRecord:
    """An access line shaped the way uvicorn builds one."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:0", "GET", path, "1.1", status),
        exc_info=None,
    )


def test_a_health_check_is_not_logged():
    assert HealthCheckFilter().filter(_access_record("/health")) is False


def test_a_webhook_is_still_logged():
    assert HealthCheckFilter().filter(_access_record("/webhooks/github")) is True


def test_an_error_is_still_logged():
    """The filter keys on the path, never on the status."""
    assert HealthCheckFilter().filter(_access_record("/nope", 404)) is True


def test_a_record_with_no_arguments_survives():
    """Our own log lines carry no args tuple and must pass untouched."""
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="shutting down",
        args=(),
        exc_info=None,
    )
    assert HealthCheckFilter().filter(record) is True


def test_only_the_exact_path_is_suppressed():
    """Deliberately narrow: anything else stays visible.

    A match on "starts with /health" would also hide a request to, say,
    /health-internal that somebody added later and expected to see.
    """
    assert HealthCheckFilter().filter(_access_record("/health?x=1")) is True
    assert HealthCheckFilter().filter(_access_record("/healthz")) is True
