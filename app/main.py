"""Servicio de Resúmenes: resúmenes de chat generados por LLM."""
import logging
from contextlib import asynccontextmanager

from typing import Annotated

from fastapi import FastAPI, Query
from pymongo import MongoClient
from starlette.concurrency import run_in_threadpool

from . import config
from .api_models import ErrorResponse, EventOut, Health
from .clients.core_client import CoreClient
from .clients.llm_client import build_provider
from .errors import register_handlers
from .events import EventBroker
from .routes import summaries
from .repository import MongoRepository, RepositoryDependency
from .worker import SummaryWorker


logging.basicConfig(level=config.LOG_LEVEL, format="%(levelname)s:%(name)s:%(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = MongoClient(config.MONGODB_URL, tz_aware=True,
                         serverSelectionTimeoutMS=config.MONGODB_TIMEOUT_MS,
                         connectTimeoutMS=config.MONGODB_TIMEOUT_MS,
                         socketTimeoutMS=config.MONGODB_TIMEOUT_MS)
    app.state.mongo = client
    try:
        await run_in_threadpool(client.admin.command, "ping")
        repository = MongoRepository(client[config.MONGODB_DATABASE])
        await run_in_threadpool(repository.ensure_indexes)
        app.state.repository = repository
        with CoreClient() as core_client:
            app.state.core_client = core_client
            worker = SummaryWorker(repository, core_client, build_provider())
            app.state.worker = worker
            broker = EventBroker(repository, on_request_received=worker.notify)
            app.state.broker = broker
            worker.on_result = broker.notify  # Publicar el evento terminal sin esperar al sondeo.
            worker.start()
            broker.start()
            try:
                yield
            finally:
                await run_in_threadpool(broker.stop)
                await run_in_threadpool(worker.stop)
    finally:
        client.close()


DESCRIPTION = """
Genera resúmenes de las conversaciones del chat con un LLM, priorizando **decisiones, eventos y tareas
asignadas**. Se pide por canal (opcionalmente acotado a un intervalo) o se dispara solo al entrar a un canal con
muchos mensajes nuevos.

## Flujo (asíncrono con polling)
1. `POST /api/v1/channels/{channel_id}/summaries` → **202** + `summary_id`.
2. `GET /api/v1/summaries/{summary_id}` hasta `messageState` = `completada` o `fallida`.

`messageState`: `pendiente` → `procesando` → `completada` | `fallida`.

## Errores
Siempre `{"detail": ...}`: **404** no existe el canal/resumen, **422** datos inválidos o usuario inexistente,
**502/503** falla el núcleo o el almacenamiento, **500** error inesperado (sin detalles internos).

## Eventos
Cada transacción emite un evento al broker: `summary.requested`, `summary.generated`, `summary.failed`.
Se pueden leer en `GET /api/v1/events`; sus esquemas JSON están en `docs/eventos/`.
"""

TAGS = [
    {"name": "summaries", "description": "Solicitar y consultar resúmenes."},
    {"name": "events", "description": "Eventos durables que el servicio publica en el broker."},
    {"name": "health", "description": "Estado del proceso."},
]

app = FastAPI(
    title="Chat Summaries",
    version="1.0.0",
    summary="Resúmenes de canales del chat generados por LLM.",
    description=DESCRIPTION,
    openapi_tags=TAGS,
    contact={"name": "summaries-service", "url": "https://github.com/marcel-silva27/summaries-service"},
    lifespan=lifespan,
)
register_handlers(app)
app.include_router(summaries.router)


@app.get("/health", tags=["health"], response_model=Health, operation_id="health", summary="Liveness",
         description="Responde `ok` si el proceso atiende. No comprueba Mongo ni RabbitMQ.")
def health():
    return {"status": "ok"}


@app.get("/api/v1/events", tags=["events"], response_model=list[EventOut], operation_id="listEvents",
         summary="Eventos durables del servicio",
         description=("Solicitudes (`summary.requested`) y resultados (`summary.generated` / `summary.failed`), del más "
                      "antiguo al más reciente. Sirven para ponerse al día si un consumidor estuvo caído."),
         responses={422: {"model": ErrorResponse, "description": "`limit` u `offset` inválidos."},
                    503: {"model": ErrorResponse, "description": "Almacenamiento no disponible."}})
def list_events(repository: RepositoryDependency,
                limit: Annotated[int, Query(ge=1, le=1000, description="Máximo de eventos.")] = 100,
                offset: Annotated[int, Query(ge=0, description="Cuántos saltar.")] = 0):
    return repository.list_events(limit=limit, offset=offset)
