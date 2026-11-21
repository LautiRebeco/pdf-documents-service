from __future__ import annotations

import threading
from collections import defaultdict
from functools import lru_cache
from typing import Mapping


class Metrics:
    """Registro minimalista, thread-safe y en memoria de contadores/gauges.

    Es suficiente para exponer el estado de los patrones de resiliencia sin
    añadir un sistema de métricas externo. En un despliegue con varios workers,
    cada proceso reporta sus propias cifras.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: defaultdict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}

    @staticmethod
    def _name(name: str, labels: Mapping[str, object]) -> str:
        if not labels:
            return name
        suffix = ".".join(f"{key}={value}" for key, value in sorted(labels.items()))
        return f"{name}.{suffix}"

    def inc(self, name: str, value: int = 1, **labels: object) -> None:
        key = self._name(name, labels)
        with self._lock:
            self._counters[key] += value

    def set_gauge(self, name: str, value: float, **labels: object) -> None:
        key = self._name(name, labels)
        with self._lock:
            self._gauges[key] = float(value)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            result: dict[str, float] = dict(self._counters)
            result.update(self._gauges)
            return result

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()


@lru_cache(maxsize=1)
def get_metrics() -> Metrics:
    return Metrics()


def reset_metrics() -> None:
    get_metrics().reset()
