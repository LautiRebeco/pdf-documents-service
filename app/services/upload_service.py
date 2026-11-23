from __future__ import annotations

import logging
import os
import socket
import time
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from pymongo.errors import DuplicateKeyError

from app.cache.document_cache import invalidate_document
from app.repositories.document_repository import DocumentRepository, get_document_repository
from app.resilience.metrics import get_metrics
from app.services.checksum_service import calc_checksum
from app.services.document_builder import construir_documento
from app.services.extract_client import InvalidPDFError, extract_text
from app.services.extract_resilience import extract_with_resilience
from app.settings import get_settings

logger = logging.getLogger(__name__)

INVALID_PDF_DETAIL = "El contenido no corresponde a un PDF valido."
PDF_MAGIC = b"%PDF-"


class DocumentProcessingInProgressError(RuntimeError):
    """Otro worker está ejecutando la saga para este checksum."""

    def __init__(self, retry_after: str = "1") -> None:
        super().__init__("El documento ya está siendo procesado.")
        self.retry_after = retry_after


class DocumentNotFoundError(RuntimeError):
    pass


def _as_result(document: dict) -> dict[str, Any]:
    return {"document_id": str(document.get("_id", "")), "document": document}


def _owner_id() -> str:
    settings = get_settings()
    return settings.saga_owner_id or f"{socket.gethostname()}:{os.getpid()}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compensate(
    repository: DocumentRepository,
    document_id: Any,
    error: Exception,
    owner: str | None = None,
) -> None:
    """Compensación de la saga: deja el documento en ``error``.

    Nunca enmascara el error original: si la compensación falla, se registra y
    el proceso de recuperación volverá a intentarla.
    """
    try:
        repository.compensate_saga(document_id, error=error, now=_now(), owner=owner)
        get_metrics().inc("saga.compensated")
        invalidate_document(document_id=str(document_id))
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("No se pudo compensar la saga de %s: %s", document_id, exc)
        get_metrics().inc("saga.compensation_failed")


def _wait_for_other_worker(
    repository: DocumentRepository,
    checksum: str,
    wait_s: float,
) -> dict | None:
    """Espera acotada a que otra réplica termine la saga del mismo checksum."""
    deadline = perf_counter() + wait_s
    while perf_counter() < deadline:
        time.sleep(min(0.05, max(0.0, deadline - perf_counter())))
        settled = repository.find_by_checksum(checksum)
        if settled is not None and settled.get("estado") in {"ok", "error"}:
            return settled
    return None


def process_pdf_upload(
    *,
    file_name: str,
    file_bytes: bytes,
    repository: DocumentRepository | None = None,
    extractor: Any = None,
) -> dict[str, Any]:
    """Ejecuta la saga de alta de documento.

    Pasos hacia delante:
      1. reservar el documento en ``pendiente`` (con lock de saga),
      2. extraer el texto con breaker + bulkhead + reintentos,
      3. completar el documento en ``ok``.

    Compensación: cualquier fallo en 2 o 3 deja el documento en ``error`` con
    el motivo, libera el lock e invalida la caché. El lock atómico-evita que dos
    workers procesen el mismo checksum simultáneamente.
    """
    started_at = perf_counter()
    settings = get_settings()
    checksum = calc_checksum(file_bytes)
    logger.info(
        "Processing PDF upload: filename=%s checksum=%s size_bytes=%d",
        file_name,
        checksum,
        len(file_bytes),
    )

    active_repository = repository or get_document_repository()
    active_extractor = extractor or extract_text
    owner = _owner_id()

    document: dict | None = None
    duplicate_error: DuplicateKeyError | None = None

    for _ in range(3):
        existing = active_repository.find_by_checksum(checksum)

        if existing is not None:
            estado = existing.get("estado")

            if estado == "error":
                claimed = active_repository.requeue_failed(
                    existing["_id"],
                    owner=owner,
                    now=_now(),
                    lock_ttl_s=settings.saga_lock_ttl_s,
                )
                if claimed is not None:
                    document = claimed
                    break
                settled = _wait_for_other_worker(
                    active_repository, checksum, settings.saga_claim_wait_s
                )
                if settled is None:
                    raise DocumentProcessingInProgressError(str(settings.saga_claim_wait_s))
                continue

            if estado == "pendiente":
                claimed = active_repository.claim_pending(
                    existing["_id"],
                    owner=owner,
                    now=_now(),
                    lock_ttl_s=settings.saga_lock_ttl_s,
                )
                if claimed is not None:
                    document = claimed
                    break
                settled = _wait_for_other_worker(
                    active_repository, checksum, settings.saga_claim_wait_s
                )
                if settled is None:
                    raise DocumentProcessingInProgressError(str(settings.saga_claim_wait_s))
                continue

            # Documento ya completado (o legacy sin campo 'estado'): idempotencia.
            logger.info(
                "Duplicate document detected: filename=%s checksum=%s existing_id=%s",
                file_name,
                checksum,
                existing.get("_id"),
            )
            return _as_result(existing)

        pending = construir_documento(
            pdf_nombre=file_name,
            texto_extraido="",
            checksum_archivo=checksum,
            duracion_ms=0,
            estado="pendiente",
        )
        pending["saga"] = {
            "attempts": 1,
            "owner": owner,
            "locked_at": _now(),
            "started_at": _now(),
            "status": "running",
        }
        try:
            inserted_id = active_repository.save_document(pending)
        except DuplicateKeyError as exc:
            # Otra réplica insertó primero: volvemos a resolver el estado.
            duplicate_error = exc
            continue

        document = {**pending, "_id": inserted_id}
        get_metrics().inc("saga.started")
        break

    if document is None:
        if duplicate_error is not None:
            raise duplicate_error
        raise DocumentProcessingInProgressError(str(settings.saga_claim_wait_s))

    document_id = document.get("_id")

    # Validación de contenido antes de reenviar bytes a otro servicio del polyrepo.
    # Se hace dentro de la saga para que la compensación siga siendo la misma.
    if PDF_MAGIC not in file_bytes[:1024]:
        invalid_pdf = InvalidPDFError(INVALID_PDF_DETAIL)
        _compensate(active_repository, document_id, invalid_pdf, owner=owner)
        raise invalid_pdf

    # Paso 2: extracción resiliente.
    try:
        texto_extraido = extract_with_resilience(file_bytes, extractor=active_extractor)
    except Exception as exc:
        _compensate(active_repository, document_id, exc, owner=owner)
        raise

    duracion_ms = int((perf_counter() - started_at) * 1000)
    logger.debug("Extracted text length=%d for filename=%s", len(texto_extraido), file_name)

    # Paso 3: cierre de la saga.
    try:
        completed = active_repository.complete_saga(
            document_id,
            texto=texto_extraido,
            duracion_ms=duracion_ms,
            now=_now(),
            owner=owner,
        )
    except Exception as exc:
        _compensate(active_repository, document_id, exc, owner=owner)
        raise

    if completed is None:
        error = DocumentNotFoundError("El documento desapareció durante la extracción.")
        _compensate(active_repository, document_id, error, owner=owner)
        raise error

    result_document = (
        completed
        if isinstance(completed, dict)
        else {
            **document,
            "estado": "ok",
            "txt_contenido": texto_extraido,
            "txt_chars": len(texto_extraido),
            "duracion_ms": duracion_ms,
            "error": None,
        }
    )

    invalidate_document(document_id=str(document_id), checksum=checksum)
    get_metrics().inc("saga.completed")

    logger.info(
        "Saved new document: filename=%s checksum=%s inserted_id=%s duration_ms=%d",
        file_name,
        checksum,
        document_id,
        duracion_ms,
    )
    return {"document_id": str(document_id), "document": result_document}
