from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_MAX_PDF_SIZE_BYTES = 10_485_760
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FORMAT = "json"
DEFAULT_EXTRACT_SERVICE_URL = "http://localhost:8001"
DEFAULT_EXTRACT_TIMEOUT_S = 30
DEFAULT_MONGO_SERVER_SELECTION_TIMEOUT_MS = 2_000
DEFAULT_CACHE_TTL_S = 300
DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
DEFAULT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_S = 30
DEFAULT_EXTRACT_MAX_ATTEMPTS = 3
DEFAULT_EXTRACT_MAX_CONCURRENCY = 20
DEFAULT_SAGA_LOCK_TTL_S = 120
DEFAULT_SAGA_RECOVERY_INTERVAL_S = 60


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        logger.debug("%s not set. Using default: %d", name, default)
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s value provided: '%s'. Falling back to default: %d", name, raw, default
        )
        return default
    if value <= 0:
        logger.warning(
            "%s must be strictly positive (got %d). Falling back to default: %d",
            name,
            value,
            default,
        )
        return default
    return value


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s value provided: '%s'. Falling back to default: %s", name, raw, default
        )
        return default
    if value <= 0:
        logger.warning(
            "%s must be strictly positive (got %s). Falling back to default: %s",
            name,
            value,
            default,
        )
        return default
    return value


def _log_level_env(name: str, default: str) -> str:
    raw = os.getenv(name, default).upper()
    if raw not in logging.getLevelNamesMapping():
        logger.warning(
            "Invalid %s value provided: '%s'. Falling back to default: %s", name, raw, default
        )
        return default
    return raw


def _choice_env(name: str, default: str, choices: set[str]) -> str:
    raw = os.getenv(name, default).lower()
    if raw not in choices:
        logger.warning(
            "Invalid %s value provided: '%s'. Falling back to default: %s", name, raw, default
        )
        return default
    return raw


@dataclass(frozen=True)
class Settings:
    app_name: str = "PDF Extractext Documents"
    max_pdf_size_bytes: int = DEFAULT_MAX_PDF_SIZE_BYTES
    max_list_limit: int = 100
    upload_spool_max_bytes: int = sys.maxsize
    log_level: str = DEFAULT_LOG_LEVEL
    log_format: str = DEFAULT_LOG_FORMAT
    shutdown_timeout_s: float = 10.0

    mongodb_uri: str = field(default="mongodb://localhost:27017", repr=False)
    mongodb_db_name: str = "pdf-extractext"
    mongodb_collection_name: str = "documents"
    mongodb_server_selection_timeout_ms: int = DEFAULT_MONGO_SERVER_SELECTION_TIMEOUT_MS

    extract_service_url: str = DEFAULT_EXTRACT_SERVICE_URL
    extract_timeout_s: int = DEFAULT_EXTRACT_TIMEOUT_S
    extract_max_attempts: int = DEFAULT_EXTRACT_MAX_ATTEMPTS
    extract_retry_base_delay_s: float = 0.2
    extract_retry_max_delay_s: float = 2.0
    extract_max_concurrency: int = DEFAULT_EXTRACT_MAX_CONCURRENCY
    circuit_breaker_failure_threshold: int = DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD
    circuit_breaker_recovery_timeout_s: int = DEFAULT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_S

    redis_url: str = field(default="", repr=False)
    cache_key_prefix: str = "pdf-documents"
    cache_ttl_s: int = DEFAULT_CACHE_TTL_S
    cache_max_entries: int = 1_000

    saga_owner_id: str = ""
    saga_lock_ttl_s: int = DEFAULT_SAGA_LOCK_TTL_S
    saga_claim_wait_s: float = 2.0
    saga_recovery_interval_s: int = DEFAULT_SAGA_RECOVERY_INTERVAL_S
    saga_recovery_batch_size: int = 20


def get_settings() -> Settings:
    logger.debug("Loading application settings from environment")

    return Settings(
        app_name=os.getenv("APP_NAME", "PDF Extractext Documents"),
        max_pdf_size_bytes=_positive_int_env("APP_MAX_PDF_SIZE_BYTES", DEFAULT_MAX_PDF_SIZE_BYTES),
        max_list_limit=_positive_int_env("APP_MAX_LIST_LIMIT", 100),
        upload_spool_max_bytes=_positive_int_env("APP_UPLOAD_SPOOL_MAX_BYTES", sys.maxsize),
        log_level=_log_level_env("APP_LOG_LEVEL", DEFAULT_LOG_LEVEL),
        log_format=_choice_env("APP_LOG_FORMAT", DEFAULT_LOG_FORMAT, {"json", "text"}),
        shutdown_timeout_s=_positive_float_env("APP_SHUTDOWN_TIMEOUT_S", 10.0),
        mongodb_uri=os.getenv("MONGODB_URI", "mongodb://localhost:27017"),
        mongodb_db_name=os.getenv("MONGODB_DB_NAME", "pdf-extractext"),
        mongodb_collection_name=os.getenv("MONGO_COLLECTION_NAME", "documents"),
        mongodb_server_selection_timeout_ms=_positive_int_env(
            "MONGODB_SERVER_SELECTION_TIMEOUT_MS", DEFAULT_MONGO_SERVER_SELECTION_TIMEOUT_MS
        ),
        extract_service_url=os.getenv("EXTRACT_SERVICE_URL", DEFAULT_EXTRACT_SERVICE_URL),
        extract_timeout_s=_positive_int_env("EXTRACT_TIMEOUT_S", DEFAULT_EXTRACT_TIMEOUT_S),
        extract_max_attempts=_positive_int_env(
            "EXTRACT_MAX_ATTEMPTS", DEFAULT_EXTRACT_MAX_ATTEMPTS
        ),
        extract_retry_base_delay_s=_positive_float_env("EXTRACT_RETRY_BASE_DELAY_S", 0.2),
        extract_retry_max_delay_s=_positive_float_env("EXTRACT_RETRY_MAX_DELAY_S", 2.0),
        extract_max_concurrency=_positive_int_env(
            "EXTRACT_MAX_CONCURRENCY", DEFAULT_EXTRACT_MAX_CONCURRENCY
        ),
        circuit_breaker_failure_threshold=_positive_int_env(
            "CIRCUIT_BREAKER_FAILURE_THRESHOLD", DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD
        ),
        circuit_breaker_recovery_timeout_s=_positive_int_env(
            "CIRCUIT_BREAKER_RECOVERY_TIMEOUT_S", DEFAULT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_S
        ),
        redis_url=os.getenv("REDIS_URL", ""),
        cache_key_prefix=os.getenv("CACHE_KEY_PREFIX", "pdf-documents"),
        cache_ttl_s=_positive_int_env("CACHE_TTL_S", DEFAULT_CACHE_TTL_S),
        cache_max_entries=_positive_int_env("CACHE_MAX_ENTRIES", 1_000),
        saga_owner_id=os.getenv("SAGA_OWNER_ID", ""),
        saga_lock_ttl_s=_positive_int_env("SAGA_LOCK_TTL_S", DEFAULT_SAGA_LOCK_TTL_S),
        saga_claim_wait_s=_positive_float_env("SAGA_CLAIM_WAIT_S", 2.0),
        saga_recovery_interval_s=_positive_int_env(
            "SAGA_RECOVERY_INTERVAL_S", DEFAULT_SAGA_RECOVERY_INTERVAL_S
        ),
        saga_recovery_batch_size=_positive_int_env("SAGA_RECOVERY_BATCH_SIZE", 20),
    )
