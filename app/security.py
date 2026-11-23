from __future__ import annotations

import hmac
import re
from fastapi import HTTPException, Request

from app.settings import get_settings

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_CHECKSUM_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._()\- ]")

MAX_RETRY_AFTER_S = 30
DEFAULT_RETRY_AFTER_S = 1

UNAUTHORIZED_DETAIL = "No autorizado."


def valid_request_id(value: str) -> bool:
    """Sólo caracteres seguros y longitud acotada para logs y headers."""
    return bool(_REQUEST_ID_RE.fullmatch(value))


def valid_checksum(value: str) -> bool:
    """Formato aceptado para la clave de caché y el path param de checksum."""
    return bool(_CHECKSUM_RE.fullmatch(value))


def safe_retry_after(raw: str | None) -> str:
    """Convierte un Retry-After externo en un entero acotado (1..30 s)."""
    if raw is None:
        return str(DEFAULT_RETRY_AFTER_S)
    try:
        seconds = int(str(raw).strip())
    except TypeError, ValueError:
        return str(DEFAULT_RETRY_AFTER_S)
    return str(max(DEFAULT_RETRY_AFTER_S, min(seconds, MAX_RETRY_AFTER_S)))


def sanitize_text(value: object, max_len: int = 200) -> str:
    """Elimina caracteres de control y acota texto que viene de terceros."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return cleaned[:max_len]


def safe_filename(value: str, max_len: int = 120) -> str:
    """Nombre de archivo seguro para Content-Disposition (ASCII, sin CRLF)."""
    base = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _FILENAME_UNSAFE_RE.sub("_", base).strip(" .")
    return (cleaned or "documento")[:max_len]


async def require_api_key(request: Request) -> None:
    """Autenticación opcional por configuración (defense-in-depth detrás de Traefik).

    Si ``APP_API_KEY`` no está configurada la API sigue funcionando como antes
    (desarrollo local). En producción el orquestador debe inyectar la clave;
    la comparación es de tiempo constante.
    """
    expected = get_settings().api_key
    if not expected:
        return

    supplied = request.headers.get("x-api-key", "")
    if not supplied:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()

    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail=UNAUTHORIZED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )
