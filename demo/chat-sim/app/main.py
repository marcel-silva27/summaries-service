"""Chat simulado para demostrar el servicio de resúmenes.

Un solo contenedor que responde como `users`, `channels` y `messages` del chat del curso (en la red
`microsvcs` tiene los alias `users`, `channels` y `messages`, puerto 8000), con los mismos contratos
pero SIN el bug del demo original: los POST de creación responden 201 y `messages` sí publica
`chat.message.created`. Además hace de Gateway simulado: `POST /api/v1/channels/{id}/enter` publica
`chat.channel.entered`. Datos en memoria con una conversación de ejemplo; se pierden al reiniciar.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pika
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger("chat-sim")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

BROKER_URL = os.environ.get("BROKER_URL", "amqp://summaries-rabbitmq:5672")

users: dict[str, dict] = {}
channels: dict[str, dict] = {}
messages: list[dict] = []

app = FastAPI(
    title="Chat simulado (demo)",
    version="1.0.0",
    description=(
        "Simula `users`, `channels` y `messages` del chat del curso y un Gateway mínimo, para probar el "
        "servicio de resúmenes. **No es el chat real**: los datos viven en memoria. Cada mensaje nuevo "
        "se publica como `chat.message.created`, y `POST /channels/{id}/enter` publica `chat.channel.entered`."))


def now() -> datetime:
    return datetime.now(timezone.utc)


def event(event_type: str, subject_id: str, data: dict, occurred_at: datetime | None = None) -> dict:
    return {"type": event_type, "subject_id": subject_id,
            "occurred_at": (occurred_at or now()).isoformat(), "data": data}


def publish(exchange: str, durable: bool, payload: dict) -> None:
    """Publica en el broker. Lanza pika.exceptions.AMQPError si no se puede."""
    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    try:
        channel = connection.channel()
        # Mismos parámetros que usan el chat del curso (chat, no durable) y el servicio (gateway, durable).
        channel.exchange_declare(exchange=exchange, exchange_type="topic", durable=durable)
        channel.basic_publish(exchange=exchange, routing_key=payload["type"], body=json.dumps(payload).encode(),
                              properties=pika.BasicProperties(content_type="application/json",
                                                              delivery_mode=2 if durable else 1))
    finally:
        connection.close()


class UserCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=80, examples=["Ana"])


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80, examples=["proyecto-final"])


class MessageCreate(BaseModel):
    author_id: str = Field(description="ID de un usuario existente.")
    content: str = Field(min_length=1, max_length=4000, examples=["Decidimos entregar el viernes."])


class Entry(BaseModel):
    user_id: str = Field(description="Usuario que entra al canal.")


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}


@app.post("/api/v1/users", status_code=status.HTTP_201_CREATED, tags=["users"], summary="Crear usuario")
def create_user(body: UserCreate):
    user = {"id": str(uuid4()), "display_name": body.display_name}
    users[user["id"]] = user
    return user


@app.get("/api/v1/users", tags=["users"], summary="Listar usuarios")
def list_users():
    return list(users.values())


@app.get("/api/v1/users/{user_id}", tags=["users"], summary="Obtener un usuario")
def get_user(user_id: str):
    if user_id not in users:
        raise HTTPException(status_code=404, detail="User not found")
    return users[user_id]


@app.post("/api/v1/channels", status_code=status.HTTP_201_CREATED, tags=["channels"], summary="Crear canal")
def create_channel(body: ChannelCreate):
    channel = {"id": str(uuid4()), "name": body.name}
    channels[channel["id"]] = channel
    return channel


@app.get("/api/v1/channels", tags=["channels"], summary="Listar canales")
def list_channels():
    return list(channels.values())


@app.get("/api/v1/channels/{channel_id}", tags=["channels"], summary="Obtener un canal")
def get_channel(channel_id: str):
    if channel_id not in channels:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channels[channel_id]


def add_message(channel_id: str, author_id: str, content: str, created_at: datetime | None = None) -> dict:
    message = {"id": str(uuid4()), "channel_id": channel_id, "author_id": author_id, "content": content,
               "created_at": (created_at or now()).isoformat()}
    messages.append(message)
    return message


@app.post("/api/v1/channels/{channel_id}/messages", status_code=status.HTTP_201_CREATED, tags=["messages"],
          summary="Publicar un mensaje",
          description="Valida canal (404) y autor (422), como el chat del curso, y publica `chat.message.created`.")
def create_message(channel_id: str, body: MessageCreate):
    if channel_id not in channels:
        raise HTTPException(status_code=404, detail="Channel not found")
    if body.author_id not in users:
        raise HTTPException(status_code=422, detail="Author does not exist")
    message = add_message(channel_id, body.author_id, body.content)
    try:
        publish("chat", False, event("chat.message.created", message["id"], message))
    except pika.exceptions.AMQPError:
        # Como el chat del curso: un broker caído no impide guardar el mensaje (el servicio reconcilia por API).
        logger.warning("No se pudo publicar chat.message.created", exc_info=True)
    return message


@app.get("/api/v1/channels/{channel_id}/messages", tags=["messages"], summary="Listar los mensajes de un canal",
         description="Como el chat del curso: lista completa, sin paginación, y `[]` aunque el canal no exista.")
def list_messages(channel_id: str):
    return [m for m in messages if m["channel_id"] == channel_id]


@app.post("/api/v1/channels/{channel_id}/enter", status_code=status.HTTP_202_ACCEPTED, tags=["gateway simulado"],
          summary="Entrar a un canal (Gateway simulado)",
          description=("Publica `chat.channel.entered` con un `entry_id` nuevo. Si el canal tiene más mensajes sin "
                       "resumir que `MESSAGE_THRESHOLD`, el servicio de resúmenes crea uno automático (consúltalo en "
                       "`GET /api/v1/channels/{id}/summaries`)."))
def enter_channel(channel_id: str, body: Entry):
    if channel_id not in channels:
        raise HTTPException(status_code=404, detail="Channel not found")
    if body.user_id not in users:
        raise HTTPException(status_code=422, detail="User does not exist")
    entry_id = str(uuid4())
    data = {"channel_id": channel_id, "user_id": body.user_id, "entry_id": entry_id}
    try:
        publish("gateway", True, event("chat.channel.entered", channel_id, data))
    except pika.exceptions.AMQPError:
        raise HTTPException(status_code=503, detail="Broker unavailable")
    return {"entry_id": entry_id}


SEED = [  # (autor, minutos atrás, texto)
    ("Ana", 170, "Buenas, ¿cómo vamos con la entrega del proyecto final?"),
    ("Beto", 165, "Falta terminar la documentación y las pruebas del módulo de pagos."),
    ("Carla", 160, "Yo creo que conviene usar PostgreSQL para los reportes."),
    ("Ana", 150, "Decidimos usar PostgreSQL para los reportes y MongoDB para el resto."),
    ("Beto", 140, "Perfecto. Yo me encargo de las pruebas del módulo de pagos."),
    ("Carla", 130, "Yo escribo la documentación de la API antes del jueves."),
    ("Ana", 100, "Recuerden que el profesor revisa las entregas el lunes a las 9:00 en la sala B."),
    ("Beto", 90, "¿Quién prepara la demo?"),
    ("Ana", 85, "Decidimos que Carla presenta la demo el lunes."),
    ("Carla", 80, "Ok, la preparo el domingo. Necesito que Beto me pase los datos de prueba."),
    ("Beto", 70, "Te los paso mañana. También acordamos congelar el alcance hoy: no se agregan funciones nuevas."),
    ("Ana", 60, "Excelente. Entonces la entrega final queda para el viernes a las 18:00."),
]


def seed() -> None:
    ids = {name: str(uuid4()) for name in ("Ana", "Beto", "Carla")}
    for name, user_id in ids.items():
        users[user_id] = {"id": user_id, "display_name": name}
    channel = {"id": str(uuid4()), "name": "proyecto-final"}
    channels[channel["id"]] = channel
    for author, minutes_ago, text in SEED:
        add_message(channel["id"], ids[author], text, now() - timedelta(minutes=minutes_ago))
    logger.info("Datos de ejemplo: canal %s (%d mensajes), usuarios %s", channel["id"], len(SEED), ids)


seed()
