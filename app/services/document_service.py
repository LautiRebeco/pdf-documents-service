from __future__ import annotations

import logging
from typing import cast

from bson.objectid import ObjectId

from app.cache.document_cache import (
    DocumentCache,
    checksum_suffix,
    document_suffix,
    get_document_cache,
    invalidate_document,
    list_suffix,
)
from app.repositories.document_repository import DocumentRepository, get_document_repository

logger = logging.getLogger(__name__)

ALLOWED_ESTADOS = {"pendiente", "ok", "error"}
ALLOWED_ESTADO_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"pendiente", "ok", "error"},
    "pendiente": {"ok", "error"},
    "error": {"pendiente"},
    "ok": set(),
}


class InvalidStatusTransitionError(ValueError):
    pass


class DocumentService:
    def __init__(
        self,
        repository: DocumentRepository | None = None,
        cache: DocumentCache | None = None,
    ) -> None:
        self.repository = repository or get_document_repository()
        self.cache = cache if cache is not None else get_document_cache()

    def list_documents(
        self,
        *,
        skip: int = 0,
        limit: int = 20,
        include_text: bool = False,
    ) -> list[dict]:
        suffix = list_suffix(skip, limit, include_text)
        cached = self.cache.get(suffix)
        if cached is not None and isinstance(cached.get("items"), list):
            return cast(list[dict], cached["items"])

        documents = self.repository.list_documents(
            skip=skip, limit=limit, include_text=include_text
        )
        self.cache.set(suffix, {"items": documents})
        return documents

    def get_document_by_id(self, document_id: ObjectId, include_text: bool = True) -> dict | None:
        suffix = document_suffix(str(document_id), include_text)
        cached = self.cache.get(suffix)
        if cached is not None:
            return cached

        document = self.repository.find_by_id(document_id, include_text=include_text)
        if document is not None:
            self.cache.set(suffix, document)
        return document

    def get_document_by_checksum(self, checksum: str) -> dict | None:
        suffix = checksum_suffix(checksum)
        cached = self.cache.get(suffix)
        if cached is not None:
            return cached

        document = self.repository.find_by_checksum(checksum)
        if document is not None:
            self.cache.set(suffix, document)
        return document

    def update_document(self, document_id: ObjectId, updates: dict) -> dict | None:
        existing = self.repository.find_by_id(document_id, include_text=False)
        if existing is None:
            return None

        expected_estado: str | None = None
        if "estado" in updates:
            self._validate_status_transition(existing.get("estado"), updates["estado"])
            expected_estado = existing.get("estado")

        updated = self.repository.update_document(
            document_id, updates, expected_estado=expected_estado
        )
        if updated is not None:
            invalidate_document(
                document_id=str(document_id), checksum=existing.get("checksum_archivo")
            )
        return updated

    def delete_document(self, document_id: ObjectId) -> bool:
        existing = self.repository.find_by_id(document_id, include_text=False)
        deleted = self.repository.delete_document(document_id)
        if deleted:
            invalidate_document(
                document_id=str(document_id),
                checksum=existing.get("checksum_archivo") if existing else None,
            )
        return deleted

    def _validate_status_transition(self, current_estado: str | None, new_estado: str) -> None:
        if new_estado not in ALLOWED_ESTADOS:
            raise InvalidStatusTransitionError(f"Estado invalido: {new_estado}")

        if current_estado == new_estado:
            return

        allowed = ALLOWED_ESTADO_TRANSITIONS.get(current_estado, ALLOWED_ESTADOS)
        if new_estado not in allowed:
            raise InvalidStatusTransitionError(
                f"Transicion de estado invalida: {current_estado} -> {new_estado}"
            )
