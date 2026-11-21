from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Sequence, cast

from redis import Redis
from redis.exceptions import RedisError

from app.resilience.metrics import Metrics

logger = logging.getLogger(__name__)


class CacheBackend(ABC):
    """Almacén de cadenas con expiración. Nunca lanza errores al llamador."""

    @abstractmethod
    def get(self, key: str) -> str | None: ...

    @abstractmethod
    def set(self, key: str, value: str, ttl_s: int) -> None: ...

    @abstractmethod
    def delete(self, keys: Sequence[str]) -> None: ...

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None: ...

    @abstractmethod
    def ping(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...


class InMemoryCache(CacheBackend):
    """Caché LRU con TTL para desarrollo o instancia única."""

    def __init__(self, max_entries: int, metrics: Metrics) -> None:
        self._max_entries = max_entries
        self._metrics = metrics
        self._lock = threading.RLock()
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def get(self, key: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._metrics.inc("cache.miss", backend="memory")
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._data.pop(key, None)
                self._metrics.inc("cache.miss", backend="memory")
                return None
            self._data.move_to_end(key)
            self._metrics.inc("cache.hit", backend="memory")
            return value

    def set(self, key: str, value: str, ttl_s: int) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl_s, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)

    def delete(self, keys: Sequence[str]) -> None:
        with self._lock:
            for key in keys:
                self._data.pop(key, None)

    def delete_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [key for key in self._data if key.startswith(prefix)]:
                self._data.pop(key, None)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        with self._lock:
            self._data.clear()


class RedisCache(CacheBackend):
    """Caché Redis con timeouts cortos y fallos silenciosos."""

    def __init__(self, url: str, metrics: Metrics, socket_timeout_s: float = 0.3) -> None:
        self._metrics = metrics
        self._client: Redis = Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=socket_timeout_s,
            socket_connect_timeout=socket_timeout_s,
            health_check_interval=30,
        )

    def get(self, key: str) -> str | None:
        try:
            value = self._client.get(key)
        except RedisError as exc:
            self._on_error("get", exc)
            return None
        self._metrics.inc("cache.hit" if value is not None else "cache.miss", backend="redis")
        return cast(str | None, value)

    def set(self, key: str, value: str, ttl_s: int) -> None:
        try:
            self._client.setex(key, ttl_s, value)
        except RedisError as exc:
            self._on_error("set", exc)

    def delete(self, keys: Sequence[str]) -> None:
        if not keys:
            return
        try:
            self._client.delete(*keys)
        except RedisError as exc:
            self._on_error("delete", exc)

    def delete_prefix(self, prefix: str) -> None:
        try:
            batch: list[str] = []
            for key in self._client.scan_iter(match=f"{prefix}*", count=200):
                batch.append(key)
                if len(batch) >= 200:
                    self._client.delete(*batch)
                    batch.clear()
            if batch:
                self._client.delete(*batch)
        except RedisError as exc:
            self._on_error("delete_prefix", exc)

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except RedisError as exc:
            self._on_error("ping", exc)
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except RedisError as exc:
            self._on_error("close", exc)

    def _on_error(self, operation: str, exc: RedisError) -> None:
        self._metrics.inc("cache.backend_error", backend="redis", operation=operation)
        logger.warning("Redis cache falló en %s: %s", operation, exc)
