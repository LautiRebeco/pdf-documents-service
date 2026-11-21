from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from bson.objectid import ObjectId
from redis.exceptions import RedisError

from app.cache import backends as backends_module
from app.cache.backends import InMemoryCache, RedisCache
from app.cache.document_cache import (
    DocumentCache,
    checksum_suffix,
    document_suffix,
    get_document_cache,
    invalidate_document,
    list_suffix,
)
from app.resilience.metrics import Metrics
from app.services.document_service import DocumentService


def test_in_memory_cache_hit_miss_and_ttl() -> None:
    cache = InMemoryCache(max_entries=10, metrics=Metrics())
    cache.set("a", "1", ttl_s=60)
    assert cache.get("a") == "1"

    cache.set("expired", "value", ttl_s=0)
    assert cache.get("expired") is None
    assert cache.get("missing") is None


def test_in_memory_cache_evicts_least_recently_used() -> None:
    cache = InMemoryCache(max_entries=2, metrics=Metrics())
    cache.set("a", "1", 60)
    cache.set("b", "2", 60)
    cache.get("a")  # 'a' pasa a ser el más reciente
    cache.set("c", "3", 60)

    assert cache.get("b") is None
    assert cache.get("a") == "1"
    assert cache.get("c") == "3"


def test_document_cache_serializes_bson_types() -> None:
    backend = InMemoryCache(max_entries=10, metrics=Metrics())
    cache = DocumentCache(backend, "test", 60, Metrics())
    document_id = ObjectId("507f1f77bcf86cd799439011")

    cache.set(
        "doc:1", {"_id": document_id, "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc)}
    )
    restored = cache.get("doc:1")

    assert restored is not None
    assert restored["_id"] == str(document_id)
    assert restored["created_at"].startswith("2024-01-01")


def test_document_cache_discards_corrupted_entries() -> None:
    backend = InMemoryCache(max_entries=10, metrics=Metrics())
    cache = DocumentCache(backend, "test", 60, Metrics())
    backend.set(cache.key("doc:1"), "{no es json", 60)

    assert cache.get("doc:1") is None
    assert backend.get(cache.key("doc:1")) is None


def test_invalidate_document_removes_document_checksum_and_list_entries() -> None:
    cache = get_document_cache()
    document_id = "507f1f77bcf86cd799439011"
    suffixes = [
        document_suffix(document_id, True),
        document_suffix(document_id, False),
        checksum_suffix("abc123"),
        list_suffix(0, 20, False),
    ]
    for suffix in suffixes:
        cache.set(suffix, {"value": suffix})

    invalidate_document(document_id=document_id, checksum="abc123")

    assert all(cache.get(suffix) is None for suffix in suffixes)


def test_redis_cache_degrades_gracefully_on_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = MagicMock()
    fake_client.get.side_effect = RedisError("redis caído")
    fake_client.setex.side_effect = RedisError("redis caído")
    fake_client.delete.side_effect = RedisError("redis caído")
    fake_client.scan_iter.side_effect = RedisError("redis caído")
    fake_client.ping.side_effect = RedisError("redis caído")
    fake_redis = MagicMock()
    fake_redis.from_url.return_value = fake_client
    monkeypatch.setattr(backends_module, "Redis", fake_redis)

    cache = RedisCache("redis://localhost:6379/0", Metrics())

    assert cache.get("k") is None
    cache.set("k", "v", 10)
    cache.delete(["k"])
    cache.delete_prefix("pref:")
    assert cache.ping() is False


def test_document_service_uses_cache_aside_and_invalidates_on_update() -> None:
    repository = MagicMock()
    document_id = ObjectId("507f1f77bcf86cd799439011")
    stored = {
        "_id": document_id,
        "checksum_archivo": "abc123",
        "estado": "pendiente",
        "txt_contenido": "texto",
    }
    repository.find_by_id.return_value = stored
    repository.update_document.return_value = {**stored, "estado": "ok"}
    service = DocumentService(repository=repository)

    first = service.get_document_by_id(document_id)
    second = service.get_document_by_id(document_id)

    assert first["estado"] == "pendiente"
    # La caché serializa a JSON: ObjectId vuelve como string.
    assert str(first["_id"]) == second["_id"]
    assert second["txt_contenido"] == first["txt_contenido"]
    repository.find_by_id.assert_called_once()

    service.update_document(document_id, {"estado": "ok"})
    service.get_document_by_id(document_id)

    assert repository.find_by_id.call_count == 3  # dos para update (find + cache miss), una inicial
