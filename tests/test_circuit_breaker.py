from __future__ import annotations

import pytest

from app.resilience.circuit_breaker import BreakerState, CircuitBreaker
from app.resilience.errors import CircuitBreakerOpenError
from app.resilience.metrics import Metrics


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(
        name="test", failure_threshold=2, recovery_timeout_s=10, metrics=Metrics(), clock=clock
    )


def test_breaker_starts_closed(breaker: CircuitBreaker) -> None:
    assert breaker.state is BreakerState.CLOSED
    assert breaker.call(lambda: 42) == 42


def test_breaker_opens_after_threshold(breaker: CircuitBreaker) -> None:
    def fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        breaker.call(fail)
    with pytest.raises(RuntimeError):
        breaker.call(fail)

    assert breaker.state is BreakerState.OPEN

    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        breaker.call(lambda: "nunca se ejecuta")

    assert exc_info.value.retry_after == "10"


def test_breaker_half_open_probe_closes_on_success(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    def fail() -> None:
        raise RuntimeError("boom")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(fail)

    clock.advance(11)
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state is BreakerState.CLOSED
    assert breaker.failure_count == 0


def test_breaker_half_open_failure_reopens(breaker: CircuitBreaker, clock: FakeClock) -> None:
    def fail() -> None:
        raise RuntimeError("boom")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(fail)

    clock.advance(11)
    with pytest.raises(RuntimeError):
        breaker.call(fail)

    assert breaker.state is BreakerState.OPEN


def test_only_one_half_open_probe_at_a_time(breaker: CircuitBreaker, clock: FakeClock) -> None:
    def fail() -> None:
        raise RuntimeError("boom")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(fail)
    clock.advance(11)
    assert breaker.state is BreakerState.HALF_OPEN

    # Simulamos una sonda en curso marcando el estado internamente.
    with breaker._lock:
        breaker._half_open_in_flight = True

    with pytest.raises(CircuitBreakerOpenError):
        breaker.call(lambda: "segunda sonda")


def test_business_errors_do_not_open_breaker(breaker: CircuitBreaker) -> None:
    class BusinessError(ValueError):
        pass

    for _ in range(5):
        with pytest.raises(BusinessError):
            breaker.call(
                lambda: (_ for _ in ()).throw(BusinessError("pdf invalido")),
                is_failure=lambda exc: not isinstance(exc, BusinessError),
            )

    assert breaker.state is BreakerState.CLOSED


def test_breaker_publishes_state_metric(breaker: CircuitBreaker) -> None:
    metrics = breaker._metrics
    assert metrics.snapshot()["breaker.state.breaker=test"] == 0

    def fail() -> None:
        raise RuntimeError("boom")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(fail)

    assert metrics.snapshot()["breaker.state.breaker=test"] == 1
    assert metrics.snapshot()["breaker.opened.breaker=test"] == 1
