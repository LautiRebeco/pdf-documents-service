from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from app.resilience.errors import CircuitBreakerOpenError
from app.resilience.metrics import Metrics

logger = logging.getLogger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit breaker thread-safe con estados closed/open/half-open.

    Un único fallo de una petición cuenta como una transición fallida, incluso
    si internamente se hicieron varios reintentos: el breaker envuelve a la
    política de reintentos, no al revés.
    """

    def __init__(
        self,
        *,
        name: str,
        failure_threshold: int,
        recovery_timeout_s: float,
        metrics: Metrics,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold debe ser >= 1")
        if recovery_timeout_s <= 0:
            raise ValueError("recovery_timeout_s debe ser > 0")

        self.name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s
        self._metrics = metrics
        self._clock = clock
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = False
        self._publish_state()

    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._maybe_allow_probe()
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failures

    def reset(self) -> None:
        with self._lock:
            self._state = BreakerState.CLOSED
            self._failures = 0
            self._opened_at = None
            self._half_open_in_flight = False
            self._publish_state()

    def call(
        self,
        fn: Callable[[], Any],
        *,
        is_failure: Callable[[Exception], bool] | None = None,
    ) -> Any:
        """Ejecuta ``fn``. ``is_failure`` permite no contar errores de negocio
        (por ejemplo un PDF inválido) como fallos del dependiente."""
        with self._lock:
            self._before_call()

        try:
            result = fn()
        except Exception as exc:
            counts_as_failure = is_failure is None or is_failure(exc)
            with self._lock:
                if counts_as_failure:
                    self._on_failure()
                else:
                    # El dependiente respondió: está sano, aunque la petición
                    # concreta haya sido rechazada por contenido inválido.
                    self._on_success()
            raise

        with self._lock:
            self._on_success()
        return result

    def _maybe_allow_probe(self) -> None:
        """Pasa de open a half-open cuando expira el período de recuperación."""
        if self._state is not BreakerState.OPEN or self._opened_at is None:
            return
        if self._clock() - self._opened_at < self._recovery_timeout_s:
            return
        self._state = BreakerState.HALF_OPEN
        self._half_open_in_flight = False
        logger.info("Circuit breaker '%s' -> half-open (sonda permitida)", self.name)
        self._metrics.inc("breaker.half_open", breaker=self.name)
        self._publish_state()

    def _before_call(self) -> None:
        self._maybe_allow_probe()
        if self._state is BreakerState.OPEN:
            assert self._opened_at is not None
            remaining = self._recovery_timeout_s - (self._clock() - self._opened_at)
            retry_after = str(max(1, math.ceil(remaining)))
            self._metrics.inc("breaker.rejected", breaker=self.name)
            raise CircuitBreakerOpenError(retry_after, self.name)
        if self._state is BreakerState.HALF_OPEN:
            if self._half_open_in_flight:
                self._metrics.inc("breaker.rejected", breaker=self.name)
                raise CircuitBreakerOpenError("1", self.name)
            self._half_open_in_flight = True

    def _on_success(self) -> None:
        was_half_open = self._state is BreakerState.HALF_OPEN
        self._failures = 0
        self._opened_at = None
        self._half_open_in_flight = False
        if was_half_open:
            logger.info("Circuit breaker '%s' -> closed (sonda exitosa)", self.name)
            self._metrics.inc("breaker.closed", breaker=self.name)
        self._state = BreakerState.CLOSED
        self._publish_state()

    def _on_failure(self) -> None:
        self._half_open_in_flight = False
        if self._state is BreakerState.HALF_OPEN:
            logger.warning("Circuit breaker '%s' falló la sonda -> open", self.name)
            self._open()
            return
        self._failures += 1
        if self._failures >= self._failure_threshold:
            logger.warning(
                "Circuit breaker '%s' abriendo tras %d fallos", self.name, self._failures
            )
            self._open()

    def _open(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._metrics.inc("breaker.opened", breaker=self.name)
        self._publish_state()

    def _publish_state(self) -> None:
        self._metrics.set_gauge(
            "breaker.state",
            {"closed": 0, "open": 1, "half_open": 2}[self._state],
            breaker=self.name,
        )
