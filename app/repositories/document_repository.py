from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, cast

from bson.objectid import ObjectId
from pymongo import MongoClient, ReturnDocument

from app.security import sanitize_text
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class DocumentRepository:
    def __init__(self, mongo_client: MongoClient | None = None, settings: Settings | None = None):
        settings = settings or get_settings()
        if mongo_client is None:
            mongo_client = MongoClient(
                settings.mongodb_uri,
                serverSelectionTimeoutMS=settings.mongodb_server_selection_timeout_ms,
            )

        self.client = mongo_client
        self.db = mongo_client[settings.mongodb_db_name]
        self.collection = self.db[settings.mongodb_collection_name]

        logger.info(
            "DocumentRepository initialized: database=%s collection=%s",
            settings.mongodb_db_name,
            settings.mongodb_collection_name,
        )

    def ensure_indexes(self) -> None:
        self.collection.create_index(
            "checksum_archivo",
            unique=True,
            partialFilterExpression={"deleted_at": {"$type": "null"}},
        )
        self.collection.create_index([("created_at", -1), ("_id", -1)])
        self.collection.create_index([("estado", 1), ("saga.locked_at", 1), ("created_at", 1)])
        logger.info("Indexes ensured on collection=%s", self.collection.name)

    def save_document(self, document: dict) -> ObjectId:
        result = self.collection.insert_one(document)
        logger.info(
            "Saved document: checksum=%s inserted_id=%s",
            document.get("checksum_archivo"),
            result.inserted_id,
        )
        inserted_id: ObjectId = result.inserted_id
        return inserted_id

    def _not_deleted_filter(self) -> dict:
        return {"$or": [{"deleted_at": None}, {"deleted_at": {"$exists": False}}]}

    def _apply_not_deleted_filter(self, base_query: dict, include_deleted: bool) -> dict:
        if include_deleted:
            return base_query

        return {"$and": [base_query, self._not_deleted_filter()]}

    def find_by_id(
        self,
        document_id: ObjectId,
        include_text: bool = True,
        include_deleted: bool = False,
    ) -> dict | None:
        logger.debug(
            "Finding document by id=%s include_text=%s include_deleted=%s",
            document_id,
            include_text,
            include_deleted,
        )
        query = self._apply_not_deleted_filter({"_id": document_id}, include_deleted)
        projection = None if include_text else {"txt_contenido": 0}
        return self.collection.find_one(query, projection)

    def find_by_checksum(self, checksum: str, include_deleted: bool = False) -> dict | None:
        logger.debug("Finding document by checksum=%s", checksum)
        query = self._apply_not_deleted_filter({"checksum_archivo": checksum}, include_deleted)
        return self.collection.find_one(query)

    def list_documents(
        self,
        *,
        skip: int = 0,
        limit: int = 20,
        include_text: bool = False,
        include_deleted: bool = False,
    ) -> list[dict]:
        logger.debug(
            "Listing documents: skip=%d limit=%d include_text=%s", skip, limit, include_text
        )
        query = {} if include_deleted else self._not_deleted_filter()
        projection = None if include_text else {"txt_contenido": 0}
        cursor = self.collection.find(query, projection)

        cursor = cursor.sort([("created_at", -1), ("_id", -1)]).skip(skip).limit(limit)
        return list(cursor)

    def update_document(
        self,
        document_id: ObjectId,
        updates: dict,
        *,
        expected_estado: str | None = None,
    ) -> dict | None:
        """Actualiza el documento. Si se indica ``expected_estado``, la
        condición forman parte del filtro atómico (evita TOCTOU)."""
        logger.debug(
            "Updating document id=%s fields=%s expected_estado=%s",
            document_id,
            list(updates.keys()),
            expected_estado,
        )
        base_query: dict[str, Any] = {"_id": document_id}
        if expected_estado is not None:
            base_query["estado"] = expected_estado
        query = self._apply_not_deleted_filter(base_query, include_deleted=False)
        return self.collection.find_one_and_update(
            query,
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
        )

    def delete_document(self, document_id: ObjectId) -> bool:
        logger.debug("Soft deleting document id=%s", document_id)
        query = self._apply_not_deleted_filter({"_id": document_id}, include_deleted=False)
        deleted_at = datetime.now(timezone.utc)
        result = self.collection.find_one_and_update(
            query,
            {"$set": {"deleted_at": deleted_at}},
            return_document=ReturnDocument.AFTER,
        )
        return result is not None

    # ------------------------------------------------------------------
    # Operaciones de la saga de upload
    # ------------------------------------------------------------------

    def _unlocked_or_stale_clause(self, now: datetime, lock_ttl_s: int) -> dict:
        cutoff = now - timedelta(seconds=lock_ttl_s)
        return {
            "$or": [
                {"saga.locked_at": None},
                {"saga.locked_at": {"$exists": False}},
                {"saga.locked_at": {"$lt": cutoff}},
            ]
        }

    def claim_pending(
        self,
        document_id: Any,
        *,
        owner: str,
        now: datetime,
        lock_ttl_s: int,
    ) -> dict | None:
        """Toma posesión atómica de una saga pendiente.

        Devuelve ``None`` si otro worker ya la está procesando o si el
        documento dejó de estar pendiente.
        """
        query = {
            "$and": [
                {"_id": document_id},
                {"estado": "pendiente"},
                self._unlocked_or_stale_clause(now, lock_ttl_s),
                self._not_deleted_filter(),
            ]
        }
        result = self.collection.find_one_and_update(
            query,
            {
                "$set": {
                    "saga.locked_at": now,
                    "saga.owner": owner,
                    "saga.updated_at": now,
                    "saga.last_error": None,
                },
                "$inc": {"saga.attempts": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if result is not None:
            logger.info("Saga claimed: document_id=%s owner=%s", document_id, owner)
        return cast(dict | None, result)

    def requeue_failed(
        self,
        document_id: Any,
        *,
        owner: str,
        now: datetime,
        lock_ttl_s: int,
    ) -> dict | None:
        """Reintenta un documento en error (compensación previa)."""
        query = {
            "$and": [
                {"_id": document_id},
                {"estado": "error"},
                self._unlocked_or_stale_clause(now, lock_ttl_s),
                self._not_deleted_filter(),
            ]
        }
        return cast(
            dict | None,
            self.collection.find_one_and_update(
                query,
                {
                    "$set": {
                        "estado": "pendiente",
                        "error": None,
                        "saga.locked_at": now,
                        "saga.owner": owner,
                        "saga.updated_at": now,
                        "saga.last_error": None,
                    },
                    "$inc": {"saga.attempts": 1},
                },
                return_document=ReturnDocument.AFTER,
            ),
        )

    def complete_saga(
        self,
        document_id: Any,
        *,
        texto: str,
        duracion_ms: int,
        now: datetime,
        owner: str | None = None,
    ) -> dict | None:
        logger.info("Completing saga for document_id=%s", document_id)
        query = self._apply_not_deleted_filter({"_id": document_id}, include_deleted=False)
        if owner is not None:
            # Fencing: el dueño original es el único que puede cerrar la saga.
            query = {"$and": [query, {"saga.owner": owner}]}
        return self.collection.find_one_and_update(
            query,
            {
                "$set": {
                    "estado": "ok",
                    "txt_contenido": texto,
                    "txt_chars": len(texto),
                    "duracion_ms": duracion_ms,
                    "error": None,
                    "saga.locked_at": None,
                    "saga.owner": None,
                    "saga.finished_at": now,
                    "saga.updated_at": now,
                    "saga.last_error": None,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    def compensate_saga(
        self,
        document_id: Any,
        *,
        error: Exception | str,
        now: datetime,
        owner: str | None = None,
    ) -> dict | None:
        detail = sanitize_text(error, 500)
        logger.warning("Compensating saga for document_id=%s: %s", document_id, detail)
        query = self._apply_not_deleted_filter({"_id": document_id}, include_deleted=False)
        if owner is not None:
            query = {"$and": [query, {"saga.owner": owner}]}
        return self.collection.find_one_and_update(
            query,
            {
                "$set": {
                    "estado": "error",
                    "error": detail,
                    "txt_contenido": "",
                    "txt_chars": 0,
                    "saga.locked_at": None,
                    "saga.owner": None,
                    "saga.last_error": detail,
                    "saga.failed_at": now,
                    "saga.updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    def find_resumable_pending(
        self,
        *,
        now: datetime,
        lock_ttl_s: int,
        limit: int,
    ) -> list[dict]:
        query = {
            "$and": [
                {"estado": "pendiente"},
                self._unlocked_or_stale_clause(now, lock_ttl_s),
                self._not_deleted_filter(),
            ]
        }
        cursor = self.collection.find(query).sort([("created_at", 1)]).limit(limit)
        return list(cursor)


@lru_cache(maxsize=1)
def get_document_repository() -> DocumentRepository:
    return DocumentRepository()


def close_document_repository() -> None:
    if get_document_repository.cache_info().currsize:
        get_document_repository().client.close()
        get_document_repository.cache_clear()
        logger.info("MongoDB client closed")
