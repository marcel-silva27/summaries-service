"""Servicio de Resúmenes: resúmenes de chat generados por LLM."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pymongo import MongoClient
from starlette.concurrency import run_in_threadpool

from . import config
from .clients.core_client import CoreClient
from .clients.llm_client import build_provider
from .errors import register_handlers
from .events import EventBroker, list_events as event_snapshot
from .routes import summaries
from .repository import MongoRepository
from .worker import SummaryWorker


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


@app.get("/api/v1/events", tags=["events"])
def list_events():
    return event_snapshot()
