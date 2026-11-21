from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from bson.objectid import ObjectId
from fastapi.testclient import TestClient
from pymongo import ReturnDocument

from app import main as main_module
from app.repositories.document_repository import DocumentRepository
from app.resilience.metrics import get_metrics
from app.services import saga_recovery, upload_service
from app.services.upload_service import DocumentProcessingInProgressError


@pytest.fixture
def repository_with_collection() -> tuple[DocumentRepository, MagicMock]:
    collection = MagicMock()
    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = collection
    return DocumentRepository(mongo_client=client), collection


def test_claim_pending_is_atomic_and_only_for_unlocked_sagas(
    repository_with_collection: tuple[DocumentRepository, MagicMock],
) -> None:
    repository, collection = repository_with_collection
    document_id = ObjectId()
    now = datetime.now(timezone.utc)
    collection.find_one_and_update.return_value = {"_id": document_id, "estado": "pendiente"}

    result = repository.claim_pending(document_id, owner="worker-1", now=now, lock_ttl_s=120)

    assert result is not None
    query, update = collection.find_one_and_update.call_args.args
    assert query["$and"][1] == {"estado": "pendiente"}
    assert query["$and"][2]["$or"][0] == {"saga.locked_at": None}
    assert update["$set"]["saga.owner"] == "worker-1"
    assert update["$inc"]["saga.attempts"] == 1
    assert (
        collection.find_one_and_update.call_args.kwargs["return_document"] is ReturnDocument.AFTER
    )


def test_complete_and_compensate_clear_the_saga_lock(
    repository_with_collection: tuple[DocumentRepository, MagicMock],
) -> None:
    repository, collection = repository_with_collection
    document_id = ObjectId()
    now = datetime.now(timezone.utc)

    repository.complete_saga(document_id, texto="texto", duracion_ms=5, now=now)
    completed_update = collection.find_one_and_update.call_args.args[1]
    assert completed_update["$set"]["estado"] == "ok"
    assert completed_update["$set"]["saga.locked_at"] is None
    assert completed_update["$set"]["txt_contenido"] == "texto"

    repository.compensate_saga(document_id, error=RuntimeError("falló"), now=now)
    compensated_update = collection.find_one_and_update.call_args.args[1]
    assert compensated_update["$set"]["estado"] == "error"
    assert compensated_update["$set"]["saga.locked_at"] is None
    assert compensated_update["$set"]["error"] == "falló"


def test_upload_rejects_while_another_worker_owns_the_saga(
    monkeypatch: pytest.MonkeyPatch, pdf_bytes: bytes
) -> None:
    repository = MagicMock()
    repository.find_by_checksum.return_value = {
        "_id": ObjectId(),
        "estado": "pendiente",
    }
    repository.claim_pending.return_value = None
    monkeypatch.setattr(upload_service, "get_document_repository", lambda: repository)

    response = TestClient(main_module.app).post(
        "/documents/upload", files={"file": ("a.pdf", pdf_bytes, "application/pdf")}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": main_module.DOCUMENT_IN_PROGRESS_ERROR_DETAIL}
    assert "retry-after" in response.headers
    with pytest.raises(DocumentProcessingInProgressError):
        upload_service.process_pdf_upload(
            file_name="a.pdf", file_bytes=pdf_bytes, repository=repository
        )


def test_upload_requeues_a_previously_compensated_document(
    monkeypatch: pytest.MonkeyPatch, pdf_bytes: bytes
) -> None:
    document_id = ObjectId("507f1f77bcf86cd799439011")
    repository = MagicMock()
    repository.find_by_checksum.return_value = {
        "_id": document_id,
        "estado": "error",
        "txt_contenido": "",
    }
    repository.requeue_failed.return_value = {
        "_id": document_id,
        "estado": "pendiente",
        "checksum_archivo": "abc",
    }
    repository.complete_saga.return_value = {
        "_id": document_id,
        "estado": "ok",
        "txt_contenido": "texto recuperado",
    }
    monkeypatch.setattr(upload_service, "get_document_repository", lambda: repository)

    result = upload_service.process_pdf_upload(
        file_name="a.pdf",
        file_bytes=pdf_bytes,
        repository=repository,
        extractor=lambda _: "texto recuperado",
    )

    repository.requeue_failed.assert_called_once()
    assert result["document"]["txt_contenido"] == "texto recuperado"


def test_complete_failure_compensates_the_saga() -> None:
    repository = MagicMock()
    repository.find_by_checksum.return_value = None
    repository.complete_saga.side_effect = RuntimeError("mongo caído")

    with pytest.raises(RuntimeError, match="mongo caído"):
        upload_service.process_pdf_upload(
            file_name="a.pdf",
            file_bytes=b"%PDF-1.4",
            repository=repository,
            extractor=lambda _: "texto",
        )

    repository.compensate_saga.assert_called_once()
    assert get_metrics().snapshot()["saga.compensated"] == 1


def test_recovery_compensates_stuck_sagas(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = MagicMock()
    document_id = ObjectId()
    repository.find_resumable_pending.return_value = [
        {"_id": document_id, "checksum_archivo": "abc123"}
    ]
    monkeypatch.setattr(saga_recovery, "get_document_repository", lambda: repository)

    recovered = saga_recovery.recover_stuck_sagas()

    assert recovered == 1
    repository.compensate_saga.assert_called_once()
    assert get_metrics().snapshot()["saga.recovery.compensated"] == 1


def test_recovery_survives_database_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = MagicMock()
    repository.find_resumable_pending.side_effect = RuntimeError("mongo caído")
    monkeypatch.setattr(saga_recovery, "get_document_repository", lambda: repository)

    assert saga_recovery.recover_stuck_sagas() == 0
