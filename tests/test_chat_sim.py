"""El chat simulado de la demo debe cumplir los contratos que el servicio de resúmenes exige del núcleo."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pika
import pytest
from fastapi.testclient import TestClient

from app.clients.core_client import CoreClient
from app.errors import ChannelNotFound, UserNotFound
from app.event_schemas import ChannelEnteredEvent, MessageCreatedEvent

SIM_PATH = Path(__file__).resolve().parent.parent / "demo" / "chat-sim" / "app" / "main.py"


@pytest.fixture
def sim(monkeypatch):
    # Se carga con otro nombre: el paquete del simulador también se llama `app`.
    spec = importlib.util.spec_from_file_location("chat_sim_under_test", SIM_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    published = []
    monkeypatch.setattr(module, "publish", lambda exchange, durable, payload: published.append((exchange, durable, payload)))
    module.published = published
    return module


@pytest.fixture
def client(sim):
    with TestClient(sim.app) as test_client:
        yield test_client


def core_client_over(client):
    def handler(request):
        response = client.request(request.method, request.url.path)
        return httpx.Response(response.status_code, content=response.content, headers={"content-type": "application/json"})
    return CoreClient(users_url="http://users", channels_url="http://channels", messages_url="http://messages",
                      transport=httpx.MockTransport(handler))


def test_seed_has_a_recent_conversation_with_decisions_and_tasks(sim, client):
    users, channels = client.get("/api/v1/users").json(), client.get("/api/v1/channels").json()
    assert sorted(u["display_name"] for u in users) == ["Ana", "Beto", "Carla"]
    assert [c["name"] for c in channels] == ["proyecto-final"]
    listed = client.get(f"/api/v1/channels/{channels[0]['id']}/messages").json()
    assert len(listed) == 12
    dates = [datetime.fromisoformat(m["created_at"]) for m in listed]
    assert dates == sorted(dates) and datetime.now(timezone.utc) - dates[0] < timedelta(hours=6)
    text = " ".join(m["content"] for m in listed).lower()
    assert "decidimos" in text and "me encargo" in text and "lunes" in text


def test_it_satisfies_the_strict_contracts_of_the_summaries_core_client(sim, client):
    core = core_client_over(client)
    channel = client.get("/api/v1/channels").json()[0]
    user = client.get("/api/v1/users").json()[0]
    assert core.get_channel(channel["id"])["name"] == "proyecto-final"
    assert core.get_user(user["id"])["display_name"] == user["display_name"]
    assert len(core.list_messages(channel["id"])) == 12
    with pytest.raises(ChannelNotFound):
        core.get_channel("no-existe")
    with pytest.raises(UserNotFound):
        core.get_user("no-existe")
    assert core.list_messages("no-existe") == []  # Como el chat del curso: lista vacía, no 404.


def test_creations_answer_201_and_validate_references_like_the_course_chat(client):
    user = client.post("/api/v1/users", json={"display_name": "Dani"})
    channel = client.post("/api/v1/channels", json={"name": "otro"})
    assert (user.status_code, channel.status_code) == (201, 201)  # El demo del curso responde 500 aquí.
    path = f"/api/v1/channels/{channel.json()['id']}/messages"
    assert client.post(path, json={"author_id": user.json()["id"], "content": "Hola"}).status_code == 201
    assert client.post(path, json={"author_id": "nadie", "content": "Hola"}).status_code == 422
    assert client.post("/api/v1/channels/nada/messages", json={"author_id": "x", "content": "Hola"}).status_code == 404
    assert client.post(path, json={"author_id": user.json()["id"], "content": ""}).status_code == 422


def test_each_new_message_is_published_as_a_valid_chat_message_created_event(sim, client):
    channel, user = client.get("/api/v1/channels").json()[0], client.get("/api/v1/users").json()[0]
    created = client.post(f"/api/v1/channels/{channel['id']}/messages",
                          json={"author_id": user["id"], "content": "Mensaje nuevo"}).json()
    exchange, durable, payload = sim.published[-1]
    assert (exchange, durable) == ("chat", False)  # Igual que el exchange del curso: no durable.
    event = MessageCreatedEvent.model_validate(payload)
    assert event.subject_id == created["id"] and event.data.content == "Mensaje nuevo"
    assert "_id" not in payload["data"]


def test_a_broker_failure_does_not_prevent_saving_a_message(sim, client, monkeypatch):
    def broken(*args):
        raise pika.exceptions.AMQPConnectionError("broker down")
    monkeypatch.setattr(sim, "publish", broken)
    channel, user = client.get("/api/v1/channels").json()[0], client.get("/api/v1/users").json()[0]
    assert client.post(f"/api/v1/channels/{channel['id']}/messages",
                       json={"author_id": user["id"], "content": "Sin broker"}).status_code == 201


def test_the_simulated_gateway_publishes_a_valid_channel_entered_event(sim, client, monkeypatch):
    channel, user = client.get("/api/v1/channels").json()[0], client.get("/api/v1/users").json()[0]
    response = client.post(f"/api/v1/channels/{channel['id']}/enter", json={"user_id": user["id"]})
    assert response.status_code == 202
    exchange, durable, payload = sim.published[-1]
    assert (exchange, durable) == ("gateway", True)
    event = ChannelEnteredEvent.model_validate(payload)
    assert event.data.entry_id == response.json()["entry_id"] and event.subject_id == channel["id"]
    assert client.post("/api/v1/channels/nada/enter", json={"user_id": user["id"]}).status_code == 404
    assert client.post(f"/api/v1/channels/{channel['id']}/enter", json={"user_id": "nadie"}).status_code == 422

    def broken(*args):
        raise pika.exceptions.AMQPConnectionError("down")
    monkeypatch.setattr(sim, "publish", broken)
    assert client.post(f"/api/v1/channels/{channel['id']}/enter", json={"user_id": user["id"]}).status_code == 503


def test_publishing_reaches_a_real_broker_with_the_same_exchange_parameters(sim, broker_url, monkeypatch):
    spec = importlib.util.spec_from_file_location("chat_sim_real_publish", SIM_PATH)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setattr(real, "BROKER_URL", broker_url)
    queue = f"test_sim_{uuid4().hex}"
    with pika.BlockingConnection(pika.URLParameters(broker_url)) as connection:
        channel = connection.channel()
        channel.exchange_declare(exchange="gateway", exchange_type="topic", durable=True)
        channel.queue_declare(queue=queue, durable=True)
        channel.queue_bind(queue=queue, exchange="gateway", routing_key="chat.channel.entered")
        payload = real.event("chat.channel.entered", "c1", {"channel_id": "c1", "user_id": "u1", "entry_id": "e1"})
        real.publish("gateway", True, payload)
        method, properties, body = channel.basic_get(queue, auto_ack=True)
        assert method is not None and json.loads(body) == payload and properties.delivery_mode == 2
        channel.queue_delete(queue=queue)
