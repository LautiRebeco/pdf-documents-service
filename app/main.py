from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from time import perf_counter
from typing import Literal

from bson.objectid import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.formparsers import MultiPartParser

from app import __version__
from app.cache.document_cache import close_document_cache, get_document_cache
from app.logging_config import bind_request_id, clear_request_id, configure_logging, get_request_id
from app.repositories.document_repository import (
    close_document_repository,
    get_document_repository,
)
from app.resilience.errors import BulkheadFullError, CircuitBreakerOpenError
from app.resilience.metrics import get_metrics
from app.services.document_service import DocumentService, InvalidStatusTransitionError
from app.services.extract_client import (
    ExtractServiceBusyError,
    ExtractServiceTimeoutError,
    ExtractServiceUnavailableError,
    InvalidPDFError,
    close_extract_client,
)
from app.services.extract_resilience import get_extract_breaker
from app.services.saga_recovery import run_saga_recovery_loop
from app.services.upload_service import (
    DocumentNotFoundError,
    DocumentProcessingInProgressError,
    process_pdf_upload,
)
from app.settings import get_settings

load_dotenv()

EMPTY_FILE_ERROR_DETAIL = "El archivo está vacio."
MAX_FILE_SIZE_ERROR_TEMPLATE = "El archivo supera el tamaño máximo permitido de {max_size} bytes."
INVALID_CONTENT_TYPE_ERROR_DETAIL = "El archivo debe enviarse como application/pdf."
INVALID_DOCUMENT_ID_ERROR_DETAIL = "ID de documento inválido."
NO_UPDATE_FIELDS_ERROR_DETAIL = "No hay campos para actualizar."
SERVICE_BUSY_ERROR_DETAIL = "Servicio saturado, reintentá en unos segundos."
DOCUMENT_IN_PROGRESS_ERROR_DETAIL = "El documento ya está siendo procesado, reintentá."

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
logger = logging.getLogger(__name__)
logger.info("Settings loaded: %s", settings)
MultiPartParser.spool_max_size = settings.upload_spool_max_bytes
MAX_LIST_LIMIT = settings.max_list_limit


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop_recovery = asyncio.Event()
    recovery_task: asyncio.Task | None = None

    # Migración de índises idempotente: la BD es propiedad de este servicio.
    try:
        await asyncio.to_thread(get_document_repository().ensure_indexes)
    except Exception as exc:
        logger.warning("No se pudieron asegurar los índices al arrancar: %s", exc)

    recovery_task = asyncio.create_task(run_saga_recovery_loop(stop_recovery))

    try:
        yield
    finally:
        stop_recovery.set()
        if recovery_task is not None:
            try:
                await asyncio.wait_for(recovery_task, timeout=settings.shutdown_timeout_s)
            except TimeoutError:
                recovery_task.cancel()
                logger.warning("La tarea de recuperación de sagas no terminó a tiempo")
        close_extract_client()
        close_document_repository()
        close_document_cache()


app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or uuid.uuid4().hex
    )
    token = bind_request_id(request_id)
    started_at = perf_counter()
    response: Response | None = None

    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "Unhandled request error",
            extra={
                "event": "http.error",
                "method": request.method,
                "path": request.url.path,
                "duration_ms": int((perf_counter() - started_at) * 1000),
            },
        )
        raise
    finally:
        logger.info(
            "HTTP request completed",
            extra={
                "event": "http.access",
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code if response is not None else 500,
                "duration_ms": int((perf_counter() - started_at) * 1000),
            },
        )
        clear_request_id(token)

    assert response is not None
    response.headers["X-Request-ID"] = request_id
    return response


class DocumentUpdate(BaseModel):
    pdf_nombre: str | None = Field(default=None, min_length=1, max_length=200)
    estado: Literal["pendiente", "ok", "error"] | None = None
    error: str | None = Field(default=None, max_length=500)


@app.get("/health")
def health() -> dict[str, str]:
    logger.debug("Health check requested")
    return {"status": "ok"}


@app.get("/health/live")
def liveness() -> dict[str, str]:
    return {"status": "ok", "request_id": get_request_id() or "-"}


@app.get("/health/ready")
def readiness() -> JSONResponse:
    checks: dict[str, str] = {}
    ready = True

    try:
        get_document_repository().client.admin.command("ping")
        checks["mongodb"] = "ok"
    except Exception as exc:
        checks["mongodb"] = f"unavailable: {type(exc).__name__}"
        ready = False

    breaker_state = get_extract_breaker().state.value
    checks["extract_breaker"] = breaker_state
    if breaker_state == "open":
        ready = False

    # La caché es degradable: se reporta pero no bloquea el readiness.
    checks["cache"] = "ok" if get_document_cache().ping() else "degraded"

    return JSONResponse(
        {"status": "ready" if ready else "unavailable", "checks": checks},
        status_code=200 if ready else 503,
    )


@app.get("/metrics")
def metrics() -> dict[str, float]:
    return get_metrics().snapshot()


def _serialize_document(document: dict) -> dict:
    document_copy = {**document}
    document_id = document_copy.get("_id")
    if document_id is not None:
        document_copy["_id"] = str(document_id)

    created_at = document_copy.get("created_at")
    if isinstance(created_at, datetime):
        document_copy["created_at"] = created_at.isoformat()

    deleted_at = document_copy.get("deleted_at")
    if isinstance(deleted_at, datetime):
        document_copy["deleted_at"] = deleted_at.isoformat()
    return document_copy


def _model_dump(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


def _parse_document_id(document_id: str) -> ObjectId:
    try:
        return ObjectId(document_id)
    except Exception as exc:
        logger.warning("Invalid document ID format: %s. Error: %s", document_id, exc)
        raise HTTPException(status_code=400, detail=INVALID_DOCUMENT_ID_ERROR_DETAIL) from exc


@app.get("/documents/by-checksum/{checksum}")
def get_document_by_checksum(checksum: str) -> dict[str, object]:
    logger.info("Requested document by checksum: %s", checksum)
    service = DocumentService()
    document = service.get_document_by_checksum(checksum)

    if document is None:
        logger.warning("Document not found for checksum: %s", checksum)
        raise HTTPException(status_code=404, detail="Documento no encontrado.")

    logger.info("Document found for checksum: %s, document_id: %s", checksum, document.get("_id"))
    return {
        "document_id": str(document.get("_id", "")),
        "document": _serialize_document(document),
    }


@app.get("/documents")
def list_documents(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=MAX_LIST_LIMIT),
    include_text: bool = False,
) -> dict[str, object]:
    logger.info("Listing documents: skip=%d limit=%d include_text=%s", skip, limit, include_text)
    service = DocumentService()
    documents = service.list_documents(skip=skip, limit=limit, include_text=include_text)

    serialized_documents = [_serialize_document(document) for document in documents]
    return {
        "items": serialized_documents,
        "count": len(serialized_documents),
        "skip": skip,
        "limit": limit,
    }


@app.get("/documents/{document_id}")
def get_document_by_id(document_id: str, include_text: bool = True) -> dict[str, object]:
    logger.info("Requested document by id: %s", document_id)
    object_id = _parse_document_id(document_id)
    service = DocumentService()
    document = service.get_document_by_id(object_id, include_text=include_text)

    if document is None:
        logger.warning("Document not found for id: %s", document_id)
        raise HTTPException(status_code=404, detail="Documento no encontrado.")

    return {
        "document_id": str(document.get("_id", "")),
        "document": _serialize_document(document),
    }


@app.patch("/documents/{document_id}")
def update_document(document_id: str, payload: DocumentUpdate) -> dict[str, object]:
    logger.info("Updating document id: %s", document_id)
    object_id = _parse_document_id(document_id)

    updates = _model_dump(payload)
    if updates.get("pdf_nombre") is None:
        updates.pop("pdf_nombre", None)
    if updates.get("estado") is None:
        updates.pop("estado", None)

    if not updates:
        raise HTTPException(status_code=400, detail=NO_UPDATE_FIELDS_ERROR_DETAIL)

    service = DocumentService()
    try:
        document = service.update_document(object_id, updates)
    except InvalidStatusTransitionError as exc:
        logger.warning("Invalid status transition for document_id=%s: %s", document_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if document is None:
        logger.warning("Update failed. Document not found for id: %s", document_id)
        raise HTTPException(status_code=404, detail="Documento no encontrado.")

    return {
        "document_id": str(document.get("_id", "")),
        "document": _serialize_document(document),
    }


@app.delete("/documents/{document_id}")
def delete_document(document_id: str) -> dict[str, str]:
    logger.info("Deleting document id: %s", document_id)
    object_id = _parse_document_id(document_id)
    service = DocumentService()
    deleted = service.delete_document(object_id)

    if not deleted:
        logger.warning("Delete failed. Document not found for id: %s", document_id)
        raise HTTPException(status_code=404, detail="Documento no encontrado.")

    return {"status": "deleted"}


@app.get("/documents/{document_id}/download")
def download_document_text(document_id: str) -> Response:
    logger.info("Download requested for document_id: %s", document_id)
    object_id = _parse_document_id(document_id)

    service = DocumentService()
    document = service.get_document_by_id(object_id, include_text=True)

    if document is None:
        logger.warning("Download failed: Document not found for ID: %s", document_id)
        raise HTTPException(status_code=404, detail="Documento no encontrado.")

    txt_content = document.get("txt_contenido", "")
    downloaded_filename = f"{document.get('pdf_nombre', 'documento')}.txt"

    logger.info(
        "Download processed successfully for document_id: %s, filename: %s",
        document_id,
        downloaded_filename,
    )

    return Response(
        content=txt_content,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{downloaded_filename}"',
        },
    )


def _ensure_valid_size(file_bytes: bytes, filename: str) -> None:
    if not file_bytes:
        logger.warning("Rejected PDF: archivo vacío. filename=%s", filename)
        raise HTTPException(status_code=400, detail=EMPTY_FILE_ERROR_DETAIL)

    if len(file_bytes) > settings.max_pdf_size_bytes:
        logger.warning(
            "Rejected PDF: archivo demasiado grande. filename=%s size_bytes=%d max_size=%d",
            filename,
            len(file_bytes),
            settings.max_pdf_size_bytes,
        )
        raise HTTPException(
            status_code=413,
            detail=MAX_FILE_SIZE_ERROR_TEMPLATE.format(max_size=settings.max_pdf_size_bytes),
        )


@app.post("/documents/upload")
async def upload_pdf(file: UploadFile = File(...)) -> dict[str, str | int]:
    file_bytes = await file.read()
    filename = file.filename or "sin_nombre.pdf"
    logger.info(
        "Upload request received: filename=%s content_type=%s size_bytes=%d",
        filename,
        file.content_type,
        len(file_bytes),
    )

    _ensure_valid_size(file_bytes, filename)

    if file.content_type != "application/pdf":
        logger.warning(
            "Upload failed: tipo de contenido inválido. filename=%s content_type=%s",
            filename,
            file.content_type,
        )
        raise HTTPException(status_code=400, detail=INVALID_CONTENT_TYPE_ERROR_DETAIL)

    try:
        result = await run_in_threadpool(
            process_pdf_upload,
            file_name=filename,
            file_bytes=file_bytes,
        )
    except InvalidPDFError as exc:
        logger.warning("Invalid PDF content for filename=%s: %s", filename, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ExtractServiceBusyError, CircuitBreakerOpenError, BulkheadFullError) as exc:
        logger.warning("Upload rejected: servicio de extracción saturado. filename=%s", filename)
        raise HTTPException(
            status_code=503,
            detail=SERVICE_BUSY_ERROR_DETAIL,
            headers={"Retry-After": exc.retry_after},
        ) from exc
    except DocumentProcessingInProgressError as exc:
        logger.warning("Upload en curso para filename=%s", filename)
        raise HTTPException(
            status_code=409,
            detail=DOCUMENT_IN_PROGRESS_ERROR_DETAIL,
            headers={"Retry-After": exc.retry_after},
        ) from exc
    except ExtractServiceTimeoutError as exc:
        logger.error("Upload failed: extracción sin respuesta. filename=%s", filename)
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except ExtractServiceUnavailableError as exc:
        logger.error("Upload failed: servicio de extracción caído. filename=%s", filename)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except DocumentNotFoundError as exc:
        logger.error("Upload failed: documento no encontrado. filename=%s", filename)
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    logger.info(
        "Upload processed successfully: filename=%s document_id=%s size_bytes=%d",
        filename,
        result["document_id"],
        len(file_bytes),
    )

    return {
        "filename": filename,
        "content_type": file.content_type,
        "size_bytes": len(file_bytes),
        "extracted_text": result["document"]["txt_contenido"],
        "status": "uploaded",
    }
