import logging

from app.config import HealthCheckFilter, SecretRedactor


def test_message_secrets_are_redacted() -> None:
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "key=super-secret", (), None
    )
    assert SecretRedactor(("super-secret",)).filter(record)
    assert record.getMessage() == "key=<redacted>"


def test_successful_health_access_log_is_suppressed() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '127.0.0.1 - "GET /api/v1/health HTTP/1.1" 200', (), None,
    )
    assert not HealthCheckFilter().filter(record)


def test_non_health_access_log_is_preserved() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '127.0.0.1 - "POST /api/v1/webhooks/radarr HTTP/1.1" 202', (), None,
    )
    assert HealthCheckFilter().filter(record)
