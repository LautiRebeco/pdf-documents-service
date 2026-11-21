from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.cache.document_cache import invalidate_document
from app.repositories.document_repository import DocumentRepository, get_document_repository
from app.resilience.metrics import get_metrics
from app.settings import get_settings

logger = logging.getLogger(__name__)


def recover_stuck_sagas(repository: DocumentRepository | None = None) -> int:
    """Compensa sagas abandonadas (worker caído, lock vencido).

    No se reintenta la extracción porque el PDF original no se persiste: la
    compensación deja la saga en ``error`` con un mensaje explicativo y el
    cliente puede re-subir el mismo archivo (el checksum garantiza
    idempotencia).
    """
    settings = get_settings()
    repo = repository or get_document_repository()
    try:
        pending = repo.find_resumable_pending(
            now=datetime.now(timezone.utc),
            lock_ttl_s=settings.saga_lock_ttl_s,
            limit=settings.saga_recovery_batch_size,
        )
    except Exception as exc:
        logger.warning("No se pudo buscar sagas pendientes: %s", exc)
        return 0

    recovered = 0
    for document in pending:
        document_id = document.get("_id")
        try:
            repo.compensate_saga(
                document_id,
                error="Saga interrumpida antes de completarse; re-subí el archivo.",
                now=datetime.now(timezone.utc),
            )
            invalidate_document(
                document_id=str(document_id),
                checksum=document.get("checksum_archivo"),
            )
            recovered += 1
            get_metrics().inc("saga.recovery.compensated")
            logger.warning("Saga huérfana compensada: document_id=%s", document_id)
        except Exception as exc:
            logger.error("Falló la compensación de %s: %s", document_id, exc)
            get_metrics().inc("saga.recovery.failed")

    if recovered:
        logger.warning("Sagas compensadas en este barrido: %d", recovered)
    return recovered


async def run_saga_recovery_loop(stop_event: asyncio.Event) -> None:
    """Barrido inicial + periódico, ejecutado fuera del event loop."""
    settings = get_settings()

    await asyncio.to_thread(recover_stuck_sagas)

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.saga_recovery_interval_s)
            break
        except TimeoutError:
            pass
        await asyncio.to_thread(recover_stuck_sagas)
