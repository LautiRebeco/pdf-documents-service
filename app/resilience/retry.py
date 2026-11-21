from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from app.resilience.metrics import Metrics

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _parse_retry_after(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def retry_call(
    fn: Callable[[], T],
    *,
    is_retryable: Callable[[Exception], bool],
    max_attempts: int,
    base_delay_s: float,
    max_delay_s: float,
    metrics: Metrics,
    retry_after_of: Callable[[Exception], str | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] | None = None,
    operation: str = "operation",
) -> T:
    """Reintenta sólo errores transitorios con backoff exponencial y jitter.

    Si el remoto indica un Retry-After mayor al techo permitido, se propaga el
    error de inmediato para que el cliente (o Traefik) decida qué hacer, en
    lugar de mantener un worker dormido demasiado tiempo.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts debe ser >= 1")
    if max_delay_s < base_delay_s:
        raise ValueError("max_delay_s debe ser >= base_delay_s")

    random_source = jitter or (lambda: random.random())
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            if attempt >= max_attempts or not is_retryable(exc):
                raise

            retry_after = _parse_retry_after(retry_after_of(exc)) if retry_after_of else None
            if retry_after is not None and retry_after > max_delay_s:
                logger.info(
                    "%s: Retry-After=%s excede el techo local (%ss), propagando",
                    operation,
                    retry_after,
                    max_delay_s,
                )
                raise

            delay = (
                retry_after
                if retry_after is not None
                else min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            )
            if retry_after is None:
                delay *= 0.5 + random_source() * 0.5

            metrics.inc("retry.attempt", operation=operation)
            logger.warning(
                "%s: reintentando tras %.3fs (intento %d/%d): %s",
                operation,
                delay,
                attempt + 1,
                max_attempts,
                exc,
            )
            sleep(delay)

    assert last_error is not None
    raise last_error
