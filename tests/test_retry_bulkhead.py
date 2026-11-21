from __future__ import annotations

import pytest

from app.resilience.bulkhead import ConcurrencyLimiter
from app.resilience.errors import BulkheadFullError
from app.resilience.metrics import Metrics
from app.resilience.retry import retry_call


def test_retry_succeeds_after_transient_failure() -> None:
    attempts = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise TimeoutError("transitorio")
        return "ok"

    result = retry_call(
        flaky,
        is_retryable=lambda exc: isinstance(exc, TimeoutError),
        max_attempts=3,
        base_delay_s=0.01,
        max_delay_s=0.02,
        metrics=Metrics(),
        sleep=lambda _: None,
        jitter=lambda: 0.5,
    )

    assert result == "ok"
    assert len(attempts) == 3


def test_retry_does_not_retry_permanent_errors() -> None:
    attempts = []

    def invalid() -> None:
        attempts.append(1)
        raise ValueError("pdf invalido")

    with pytest.raises(ValueError):
        retry_call(
            invalid,
            is_retryable=lambda exc: isinstance(exc, TimeoutError),
            max_attempts=3,
            base_delay_s=0.01,
            max_delay_s=0.02,
            metrics=Metrics(),
            sleep=lambda _: None,
        )

    assert len(attempts) == 1


def test_retry_propagates_immediately_when_retry_after_is_too_long() -> None:
    sleeps = []
    attempts = []

    def busy() -> None:
        attempts.append(1)
        raise RuntimeError("busy")

    with pytest.raises(RuntimeError):
        retry_call(
            busy,
            is_retryable=lambda exc: isinstance(exc, RuntimeError),
            retry_after_of=lambda exc: "120",
            max_attempts=3,
            base_delay_s=0.01,
            max_delay_s=1.0,
            metrics=Metrics(),
            sleep=sleeps.append,
        )

    assert len(attempts) == 1
    assert sleeps == []


def test_bulkhead_rejects_when_full_and_releases_after_use() -> None:
    limiter = ConcurrencyLimiter(name="test", max_concurrency=1, metrics=Metrics())

    with limiter.acquire():
        assert limiter.in_flight == 1
        with pytest.raises(BulkheadFullError):
            with limiter.acquire():
                pass

    assert limiter.in_flight == 0
    with limiter.acquire():
        assert limiter.in_flight == 1
