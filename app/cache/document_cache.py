from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from functools import lru_cache
from typing import Any

from bson.objectid import ObjectId

from app.cache.backends import CacheBackend, InMemoryCache, RedisCache
from app.resilience.metrics import Metrics, get_metrics
from app.security import valid_checksum
from app.settings import get_settings

logger = logging.getLogger(__name__)


def _json_default(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Tipo no serializable en caché: {type(value)!r}")


class DocumentCache:
    """Cache-aside tipada para documentos, con claves versionadas."""

    def __init__(self, backend: CacheBackend, prefix: str, ttl_s: int, metrics: Metrics) -> None:
        self.backend = backend
        self._prefix = f"{prefix}:v1"
        self._ttl_s = ttl_s
        self._metrics = metrics

    def key(self, suffix: str) -> str:
        return f"{self._prefix}:{suffix}"

    def get(self, suffix: str) -> dict[str, Any] | None:
        key = self.key(suffix)
        raw = self.backend.get(key)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning("Entrada de caché corrupta en %s: %s", key, exc)
            self._metrics.inc("cache.invalid", backend=type(self.backend).__name__)
            self.backend.delete([key])
            return None
        if not isinstance(value, dict):
            self.backend.delete([key])
            return None
        return value

    def set(self, suffix: str, value: dict[str, Any]) -> None:
        try:
            raw = json.dumps(value, default=_json_default)
        except (TypeError, ValueError) as exc:
            logger.warning("No se pudo cachear %s: %s", suffix, exc)
            self._metrics.inc("cache.serialize_error")
            return
        self.backend.set(self.key(suffix), raw, self._ttl_s)

    def delete(self, *suffixes: str) -> None:
        self.backend.delete([self.key(suffix) for suffix in suffixes])

    def delete_prefix(self, suffix_prefix: str) -> None:
        self.backend.delete_prefix(self.key(suffix_prefix))

    def ping(self) -> bool:
        return self.backend.ping()

    def close(self) -> None:
        self.backend.close()


def document_suffix(document_id: str, include_text: bool) -> str:
    return f"doc:{document_id}:text={int(include_text)}"


def checksum_suffix(checksum: str) -> str:
    # Entrada controlada por el cliente: si no es un checksum válido se deriva
    # una clave opaca para no permitir colisiones ni inyección de namespace.
    if valid_checksum(checksum):
        return f"checksum:{checksum}"
    digest = hashlib.sha256(checksum.encode("utf-8")).hexdigest()
    return f"checksum:invalid:{digest}"


def list_suffix(skip: int, limit: int, include_text: bool) -> str:
    return f"list:{skip}:{limit}:{int(include_text)}"


def invalidate_document(document_id: str, checksum: str | None = None) -> None:
    """Invalida todas las representaciones cacheadas de un documento."""
    cache = get_document_cache()
    suffixes = [
        document_suffix(document_id, include_text=True),
        document_suffix(document_id, include_text=False),
    ]
    if checksum:
        suffixes.append(checksum_suffix(checksum))
    cache.delete(*suffixes)
    cache.delete_prefix("list:")


@lru_cache(maxsize=1)
def get_document_cache() -> DocumentCache:
    settings = get_settings()
    metrics = get_metrics()
    if settings.redis_url:
        logger.info("Usando caché Redis (ttl=%ds)", settings.cache_ttl_s)
        backend: CacheBackend = RedisCache(settings.redis_url, metrics)
    else:
        logger.info("Usando caché en memoria (ttl=%ds)", settings.cache_ttl_s)
        backend = InMemoryCache(settings.cache_max_entries, metrics)
    return DocumentCache(backend, settings.cache_key_prefix, settings.cache_ttl_s, metrics)


def close_document_cache() -> None:
    if get_document_cache.cache_info().currsize:
        get_document_cache().close()
        get_document_cache.cache_clear()
        logger.info("Caché cerrada")
