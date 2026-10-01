"""Servicio de Resúmenes: resúmenes de chat generados por LLM."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pymongo import MongoClient

from . import config
from .errors import register_handlers
from .events import events
from .routes import summaries


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.mongo = MongoClient(config.MONGODB_URL)
    # TODO (paso 6/7): lanzar consumidores de chat.message.created y summary.requested.
    yield
    app.state.mongo.close()


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
    return events
