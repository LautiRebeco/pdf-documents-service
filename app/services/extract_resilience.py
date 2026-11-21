from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, cast

from app.resilience.bulkhead import ConcurrencyLimiter
from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.errors import CircuitBreakerOpenError
from app.resilience.metrics import get_metrics
from app.resilience.retry import retry_call
from app.services.extract_client import (
    ExtractServiceBusyError,
    ExtractServiceTimeoutError,
    ExtractServiceUnavailableError,
    InvalidPDFError,
    extract_text,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)

TRANSIENT_EXTRACT_ERRORS = (
    ExtractServiceBusyError,
    ExtractServiceTimeoutError,
    ExtractServiceUnavailableError,
)


@lru_cache(maxsize=1)
def get_extract_breaker() -> CircuitBreaker:
    settings = get_settings()
    return CircuitBreaker(
        name="extract",
        failure_threshold=settings.circuit_breaker_failure_threshold,
        recovery_timeout_s=settings.circuit_breaker_recovery_timeout_s,
        metrics=get_metrics(),
    )


@lru_cache(maxsize=1)
def get_extract_bulkhead() -> ConcurrencyLimiter:
    settings = get_settings()
    return ConcurrencyLimiter(
        name="extract",
        max_concurrency=settings.extract_max_concurrency,
        metrics=get_metrics(),
    )


def reset_extract_resilience() -> None:
    """Útil para tests y para el arranque limpio de un worker."""
    get_extract_breaker().reset()
    get_extract_bulkhead()


def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, TRANSIENT_EXTRACT_ERRORS)


def _retry_after_of(exc: Exception) -> str | None:
    if isinstance(exc, ExtractServiceBusyError):
        return exc.retry_after
    return None


def _is_breaker_failure(exc: Exception) -> bool:
    # Un PDF inválido es una respuesta correcta del servicio de extracción:
    # no debe abrir el circuit breaker.
    return not isinstance(exc, InvalidPDFError) and not isinstance(exc, CircuitBreakerOpenError)


def extract_with_resilience(
    file_bytes: bytes,
    *,
    extractor: Any = None,
) -> str:
    """Compone bulkhead -> circuit breaker -> reintentos sobre el extractor.

    El orden importa: el bulkhead rechaza antes de consumir el breaker, el
    breaker evita llamadas inútiles durante una caída, y los reintentos quedan
    dentro del breaker para que una petición sume como máximo un fallo.
    """
    settings = get_settings()
    active_extractor = extractor or extract_text

    def attempt() -> str:
        return retry_call(
            lambda: active_extractor(file_bytes),
            is_retryable=_is_retryable,
            retry_after_of=_retry_after_of,
            max_attempts=settings.extract_max_attempts,
            base_delay_s=settings.extract_retry_base_delay_s,
            max_delay_s=settings.extract_retry_max_delay_s,
            metrics=get_metrics(),
            operation="extract_text",
        )

    with get_extract_bulkhead().acquire():
        return cast(str, get_extract_breaker().call(attempt, is_failure=_is_breaker_failure))
