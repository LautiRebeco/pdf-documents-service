from __future__ import annotations

import pytest

from app.cache.document_cache import get_document_cache
from app.repositories.document_repository import get_document_repository
from app.resilience.metrics import reset_metrics
from app.services.extract_resilience import reset_extract_resilience


@pytest.fixture
def pdf_bytes() -> bytes:
    # La extracción vive en el servicio extract: acá alcanza con bytes que parezcan un PDF.
    return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


@pytest.fixture(autouse=True)
def isolate_resilience_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla breaker, caché y métricas entre tests y acelera los reintentos."""
    monkeypatch.setenv("EXTRACT_RETRY_BASE_DELAY_S", "0.001")
    monkeypatch.setenv("EXTRACT_RETRY_MAX_DELAY_S", "0.002")
    monkeypatch.setenv("SAGA_CLAIM_WAIT_S", "0.001")

    reset_extract_resilience()
    reset_metrics()
    get_document_cache.cache_clear()
    get_document_repository.cache_clear()

    yield

    reset_extract_resilience()
    get_document_cache.cache_clear()
    get_document_repository.cache_clear()
