# pdf-documents-service

Microservicio de documentos del poly repo **PDF Extract**. Es **el único componente con acceso a MongoDB**: el resto de los servicios deben usar esta API para persistir, consultar y descargar documentos.

- Runtime: Python 3.14 / FastAPI / MongoDB / Redis (opcional pero recomendado).
- Persistencia: deduplicación por `sha256`, borrado lógico y máquina de estados `pendiente → ok | error`.
- Resiliencia: saga de upload, circuit breaker, bulkhead, reintentos con backoff y caché cache-aside.

---

## Estado de los 12 Factor App

| # | Factor | Cómo se cumple |
|---|---|---|
| I | Codebase | Un repo, un servicio, dependencias fijadas en `uv.lock`. |
| II | Dependencias | Declaradas en `pyproject.toml`; el Dockerfile instala con `uv sync --frozen`. Versión de `uv` fijada (`UV_VERSION`). |
| III | Config | Toda la configuración disponible por variables de entorno (`app/settings.py`, ver `.env.example`). Sin secretos en el código; `mongodb_uri`, `redis_url` y contraseñas se ocultan en `repr`. |
| IV | Backing services | MongoDB, Redis y el servicio extract son recursos adjuntos configurables por URL. |
| V | Build, release, run | Imagen multi-stage: builder → runtime. `APP_VERSION` como etiqueta OCI, arranque `exec uvicorn`, `PORT` y `WEB_CONCURRENCY` por entorno. |
| VI | Procesos | Procesos stateless. Los clientes (Mongo/httpx) son pools compartidos, no estado de negocio; el estado de la saga vive en MongoDB. |
| VII | Port binding | Uvicorn hace bind a `0.0.0.0:$PORT` dentro del contenedor. |
| VIII | Concurrencia | Requests sync en threadpool + `run_in_threadpool`; locks atómicos en Mongo para sagas concurrentes; bulkhead limita la presión sobre el extract. |
| IX | Disposabilidad | `/health/live` y `/health/ready`, `HEALTHCHECK` en la imagen, cierre ordenado de clientes y de la tarea de recuperación en `lifespan`. |
| X | Dev/prod parity | `docker-compose.dev.yml`, `.env.example` y creación idempotente de índices en el arranque (antes era un script manual). |
| XI | Logs | Logs JSON a stdout con `request_id`, duración, método y status por request; nivel y formato configurables. |
| XII | Admin processes | `app/admin/create_indexes.py` sigue disponible como tarea one-off con la misma configuración. |

---

## Patrones de resiliencia

### Saga de upload

`POST /documents/upload` ejecuta una saga con tres pasos y compensación:

1. **Reservar**: se inserta el documento en `estado="pendiente"` con un lock de saga (`owner`, `locked_at`, `attempts`). El lock se reclama con `find_one_and_update` atómico: si otro worker ya lo tiene, se espera un intervalo acotado y se devuelve `409 + Retry-After`.
2. **Extraer**: llamada al servicio extract con circuit breaker + bulkhead + reintentos.
3. **Completar**: se escribe el texto y se pasa a `estado="ok"`, liberando el lock e invalidando la caché.

Si falla el paso 2 o el 3, la **compensación** marca el documento como `error`, guarda el motivo, libera el lock e invalida la caché. Una tarea de recuperación compensa en el arranque y periódicamente las sagas huérfanas (lock vencido / worker caído).

> **Limitación deliberada**: el PDF original no se persiste, por lo que la recuperación no puede repetir la extracción; compensa a `error` y el cliente puede re-subir el mismo archivo. El `checksum` único garantiza que ese re-intento sea idempotente y no duplique documentos.

### Circuit breaker

Envoltorio del extract (`app/services/extract_resilience.py`) con estados `closed → open → half_open`:

- `CIRCUIT_BREAKER_FAILURE_THRESHOLD` fallos consecutivos abren el breaker.
- Tras `CIRCUIT_BREAKER_RECOVERY_TIMEOUT_S` se permite una sonda; si falla vuelve a abrirse.
- Un `InvalidPDFError` **no** cuenta como fallo del dependiente (es una respuesta válida).
- Abierto o saturado → `503 + Retry-After`, sin salir hacia el extract.
- Consultable en `/health/ready` y `/metrics`.

### Bulkhead y reintentos

- `EXTRACT_MAX_CONCURRENCY` limita las llamadas simultáneas al extract; el exceso se rechaza de inmediato con `503 + Retry-After`.
- Reintentos exponenciales con jitter para timeouts y errores de transporte. Si el extract devuelve `Retry-After` mayor al techo local (`EXTRACT_RETRY_MAX_DELAY_S`), se propaga de inmediato para no dormir un worker.
- Los errores de contenido (PDF inválido) nunca se reintentan.

### Caché

- **Cache-aside** en las consultas por id, checksum y listados.
- Backend Redis cuando se define `REDIS_URL` (recomendado con más de un worker / detrás de Traefik); caché LRU en memoria en caso contrario.
- TTL (`CACHE_TTL_S`) e invalidación explícita en upload, PATCH y DELETE.
- Si Redis falla, se sigue sirviendo desde MongoDB y el evento se registra en `/metrics` (`cache.backend_error`): la caché nunca es un punto único de fallo.

---

## API

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Health legado (siempre `ok`). |
| GET | `/health/live` | Liveness del proceso. |
| GET | `/health/ready` | Mongo + breaker; `503` si no está listo. La caché se reporta pero no bloquea. |
| GET | `/metrics` | Contadores/gauges de breaker, bulkhead, reintentos, caché y sagas. |
| POST | `/documents/upload` | Sube un PDF y ejecuta la saga. |
| GET | `/documents` | Lista documentos (paginada). |
| GET | `/documents/{id}` | Obtiene un documento. |
| GET | `/documents/by-checksum/{checksum}` | Busca por checksum. |
| GET | `/documents/{id}/download` | Descarga el texto extraído. |
| PATCH | `/documents/{id}` | Actualiza campos/estado (transiciones atómicas). |
| DELETE | `/documents/{id}` | Borrado lógico. |

Todas las respuestas incluyen `X-Request-ID`. Si Traefik envía `X-Request-ID` o `X-Correlation-ID`, se reutiliza para correlacionar trazas entre servicios.

Códigos de error relevantes: `400` PDF inválido, `409` saga en curso, `503` extract saturado o breaker abierto, `504` timeout del extract, `502` extract caído.

---

## Desarrollo y despliegue

```bash
uv sync --frozen
uv run pytest -q
docker compose -f docker-compose.dev.yml up --build
```

Copiar `.env.example` a `.env` y ajustar. En producción, el repo orquestador debe proveer:

- `MONGODB_URI` (acceso exclusivo de este servicio).
- `REDIS_URL` compartida por todas las réplicas.
- `EXTRACT_SERVICE_URL` alcanzable desde la red de Traefik.
- Rutas de healthcheck: `/health/live` (liveness) y `/health/ready` (readiness).
- Reenvío/generación de `X-Request-ID` en Traefik para correlación extremo a extremo.

Con `WEB_CONCURRENCY>1`, el circuit breaker y el bulkhead son por proceso; la caché debe ser Redis para que la invalidación sea coherente entre workers.
