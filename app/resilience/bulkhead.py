from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from app.resilience.errors import BulkheadFullError
from app.resilience.metrics import Metrics

logger = logging.getLogger(__name__)


class ConcurrencyLimiter:
    """Bulkhead: limita las operaciones concurrentes hacia un recurso.

    El rechazo es inmediato (no hay espera en la request) para liberar al
    hilo/worker y devolver un 503 con Retry-After en lugar de acumular
    peticiones bloqueadas.
    """

    def __init__(self, *, name: str, max_concurrency: int, metrics: Metrics) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency debe ser >= 1")
        self.name = name
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self._metrics = metrics
        self._lock = threading.Lock()
        self._in_flight = 0
        self._metrics.set_gauge("bulkhead.in_flight", 0, bulkhead=name)

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @contextmanager
    def acquire(self) -> Iterator[None]:
        if not self._semaphore.acquire(blocking=False):
            logger.warning("Bulkhead '%s' saturado (rechazando petición)", self.name)
            self._metrics.inc("bulkhead.rejected", bulkhead=self.name)
            raise BulkheadFullError(bulkhead_name=self.name)

        with self._lock:
            self._in_flight += 1
            self._metrics.set_gauge("bulkhead.in_flight", self._in_flight, bulkhead=self.name)
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1
                self._metrics.set_gauge("bulkhead.in_flight", self._in_flight, bulkhead=self.name)
            self._semaphore.release()
