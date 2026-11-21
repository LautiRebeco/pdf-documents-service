from __future__ import annotations


class ResilienceError(RuntimeError):
    """Error base para fallos rápidos de los patrones de resiliencia."""


class CircuitBreakerOpenError(ResilienceError):
    """El breaker está abierto: la llamada se rechaza sin salir."""

    def __init__(self, retry_after: str, breaker_name: str = "extract") -> None:
        super().__init__(f"Circuit breaker '{breaker_name}' abierto.")
        self.retry_after = retry_after
        self.breaker_name = breaker_name


class BulkheadFullError(ResilienceError):
    """No hay capacidad disponible para otra operación concurrente."""

    def __init__(self, retry_after: str = "1", bulkhead_name: str = "extract") -> None:
        super().__init__(f"Bulkhead '{bulkhead_name}' saturado.")
        self.retry_after = retry_after
        self.bulkhead_name = bulkhead_name
