from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import httpx
import pytest
from bson.objectid import ObjectId
from fastapi.testclient import TestClient

from app import main as main_module
from app.cache.document_cache import checksum_suffix
from app.security import safe_filename, safe_retry_after, valid_request_id
from app.services import upload_service
from app.services.extract_client import (
    ExtractServiceBusyError,
    ExtractServiceUnavailableError,
    InvalidPDFError,
    extract_text,
)
from app.settings import get_settings

client = TestClient(main_module.app)


def test_api_key_is_enforced_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_KEY", "clave-secreta")
    monkeypatch.setattr(main_module, "DocumentService", lambda: MagicMock())

    assert client.get("/documents").status_code == 401
    ok = client.get("/documents", headers={"X-API-Key": "clave-secreta"})
    assert ok.status_code == 200
    bearer = client.get("/documents", headers={"Authorization": "Bearer clave-secreta"})
    assert bearer.status_code == 200
    # Los endpoints de sonda siguen abiertos para los healthchecks.
    assert client.get("/health/live").status_code == 200


def test_api_key_is_never_logged_in_settings_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_KEY", "clave-secreta")
    assert "clave-secreta" not in repr(get_settings())


def test_request_id_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    assert valid_request_id("traza-desde-traefik")
    assert not valid_request_id("x" * 65)

    response = client.get("/health", headers={"X-Request-ID": "x" * 100})
    assert len(response.headers["x-request-id"]) == 32


def test_serializer_never_exposes_saga_internals() -> None:
    payload = main_module._serialize_document(
        {"_id": ObjectId(), "pdf_nombre": "a.pdf", "saga": {"owner": "host:123"}}
    )
    assert "saga" not in payload
    assert payload["pdf_nombre"] == "a.pdf"


def test_download_filename_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    service = MagicMock()
    service.get_document_by_id.return_value = {
        "_id": ObjectId(),
        "pdf_nombre": 'malo"; filename="evil.exe',
        "txt_contenido": "texto",
    }
    monkeypatch.setattr(main_module, "DocumentService", lambda: service)

    response = client.get("/documents/507f1f77bcf86cd799439011/download")

    disposition = response.headers["content-disposition"]
    assert 'filename="evil.exe' not in disposition
    assert "\r" not in disposition and "\n" not in disposition
    assert disposition.endswith('.txt"')
    assert safe_filename("中文.pdf").isascii()
    assert safe_filename("中文.pdf").endswith(".pdf")


def test_checksum_path_is_validated() -> None:
    response = client.get("/documents/by-checksum/no!es-un-checksum")
    assert response.status_code == 400
    assert response.json() == {"detail": main_module.INVALID_CHECKSUM_ERROR_DETAIL}
    assert checksum_suffix("no!es-un-checksum").startswith("checksum:invalid:")


def test_retry_after_is_bounded() -> None:
    assert safe_retry_after("999") == "30"
    assert safe_retry_after("basura") == "1"
    assert ExtractServiceBusyError("999").retry_after == "30"


def test_non_pdf_bytes_are_rejected_inside_the_saga() -> None:
    repository = MagicMock()
    repository.find_by_checksum.return_value = None

    with pytest.raises(InvalidPDFError):
        upload_service.process_pdf_upload(
            file_name="a.pdf", file_bytes=b"esto no es un pdf", repository=repository
        )

    repository.save_document.assert_called_once()
    repository.compensate_saga.assert_called_once()


def test_oversized_declared_body_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_module, "settings", replace(main_module.settings, max_pdf_size_bytes=10)
    )
    files = {"file": ("a.pdf", b"%PDF-1.4" + b"x" * 2048, "application/pdf")}

    response = client.post("/documents/upload", files=files)

    assert response.status_code == 413


def test_rate_limit_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module._rate_buckets.clear()
    monkeypatch.setattr(main_module, "DocumentService", lambda: MagicMock())
    monkeypatch.setattr(
        main_module, "settings", replace(main_module.settings, rate_limit_per_minute=2)
    )
    try:
        assert client.get("/documents").status_code == 200
        assert client.get("/documents").status_code == 200
        limited = client.get("/documents")
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"
    finally:
        main_module._rate_buckets.clear()


def test_upstream_oversized_payload_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": "x" * (5_000_000 + 1)})

    with pytest.raises(ExtractServiceUnavailableError):
        extract_text(
            b"%PDF-1.4",
            client=httpx.Client(base_url="http://extract", transport=httpx.MockTransport(handler)),
        )
