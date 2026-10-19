"""Servicio de Resúmenes: resúmenes de chat generados por LLM."""
import logging
from contextlib import asynccontextmanager

from typing import Annotated

from fastapi import FastAPI, Query
from pymongo import MongoClient
from starlette.concurrency import run_in_threadpool

from . import config
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


app = FastAPI(
    title="Chat Summaries",
    version="1.0.0",
    description="Genera resúmenes de canales del chat. POST responde 202; "
                "consulta GET /api/v1/summaries/{id} hasta `completada` o `fallida`.",
    lifespan=lifespan,
)
register_handlers(app)
app.include_router(summaries.router)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}


@app.get("/api/v1/events", tags=["events"],
         summary="Eventos durables del servicio (solicitudes y resultados), más antiguos primero")
def list_events(repository: RepositoryDependency, limit: Annotated[int, Query(ge=1, le=1000)] = 100,
                offset: Annotated[int, Query(ge=0)] = 0):
    return repository.list_events(limit=limit, offset=offset)
