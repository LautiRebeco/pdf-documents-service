from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app import main as main_module
from app.cache.document_cache import get_document_cache
from app.logging_config import JsonFormatter, bind_request_id, clear_request_id
from app.repositories.document_repository import get_document_repository
from app.services.extract_resilience import get_extract_breaker

client = TestClient(main_module.app)


def test_request_id_is_generated_and_returned() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert len(response.headers["x-request-id"]) == 32


def test_request_id_from_traefik_is_propagated() -> None:
    response = client.get("/health", headers={"X-Request-ID": "traza-desde-traefik"})

    assert response.headers["x-request-id"] == "traza-desde-traefik"


def test_json_logs_include_request_id() -> None:
    token = bind_request_id("request-123")
    try:
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="mensaje %s",
            args=("de prueba",),
            exc_info=None,
        )
        record.request_id = "request-123"
        payload = json.loads(JsonFormatter().format(record))
    finally:
        clear_request_id(token)

    assert payload["request_id"] == "request-123"
    assert payload["message"] == "mensaje de prueba"
    assert payload["level"] == "INFO"


def test_liveness_and_metrics_endpoints() -> None:
    live = client.get("/health/live")
    metrics = client.get("/metrics")

    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert metrics.status_code == 200
    assert isinstance(metrics.json(), dict)


def test_readiness_is_200_when_mongo_is_available(monkeypatch) -> None:
    repository = MagicMock()
    repository.client.admin.command.return_value = {"ok": 1}
    monkeypatch.setattr(main_module, "get_document_repository", lambda: repository)

    cache = MagicMock()
    cache.ping.return_value = True
    monkeypatch.setattr(main_module, "get_document_cache", lambda: cache)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["checks"]["mongodb"] == "ok"


def test_readiness_is_503_when_mongo_is_down(monkeypatch) -> None:
    repository = MagicMock()
    repository.client.admin.command.side_effect = RuntimeError("mongo caído")
    monkeypatch.setattr(main_module, "get_document_repository", lambda: repository)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_readiness_is_503_when_extract_breaker_is_open(monkeypatch) -> None:
    repository = MagicMock()
    repository.client.admin.command.return_value = {"ok": 1}
    monkeypatch.setattr(main_module, "get_document_repository", lambda: repository)

    breaker = MagicMock()
    breaker.state.value = "open"
    monkeypatch.setattr(main_module, "get_extract_breaker", lambda: breaker)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["extract_breaker"] == "open"


def test_cache_degradation_does_not_break_readiness(monkeypatch) -> None:
    repository = MagicMock()
    repository.client.admin.command.return_value = {"ok": 1}
    monkeypatch.setattr(main_module, "get_document_repository", lambda: repository)

    cache = MagicMock()
    cache.ping.return_value = False
    monkeypatch.setattr(main_module, "get_document_cache", lambda: cache)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"]["cache"] == "degraded"


def test_global_singletons_can_be_recreated() -> None:
    # Los tests de observabilidad no deben depender del orden de ejecución.
    assert get_document_cache() is get_document_cache()
    assert get_extract_breaker() is get_extract_breaker()
    assert get_document_repository() is get_document_repository()
