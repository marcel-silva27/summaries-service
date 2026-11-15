"""Integración: servicios reales del chat base y de resúmenes en Docker Compose (compose.integration.yaml)."""
import os
import time
from uuid import uuid4

import httpx
import pytest

URLS = {name: os.environ[f"{name.upper()}_URL"] for name in ("users", "channels", "messages", "summaries")}


@pytest.fixture(scope="session")
def http():
    with httpx.Client(timeout=10) as client:
        yield client


@pytest.fixture
def chat(http):
    """Crea un usuario y un canal propios en el núcleo real; cada prueba usa datos nuevos (UUID en el nombre)."""
    user = http.post(f"{URLS['users']}/api/v1/users", json={"display_name": f"Ana-{uuid4().hex[:6]}"})
    assert user.status_code == 201, user.text
    channel = http.post(f"{URLS['channels']}/api/v1/channels", json={"name": f"general-{uuid4().hex[:6]}"})
    assert channel.status_code == 201, channel.text
    return {"user": user.json(), "channel": channel.json()}


def send_messages(http, chat, texts):
    for text in texts:
        response = http.post(f"{URLS['messages']}/api/v1/channels/{chat['channel']['id']}/messages",
                             json={"author_id": chat["user"]["id"], "content": text})
        assert response.status_code == 201, response.text


def wait_for(http, url, done, timeout=60):
    """Consulta (polling) hasta que `done(respuesta_json)` sea verdadero; falla con la última respuesta."""
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        response = http.get(url)
        last = response.json() if response.status_code == 200 else response.status_code
        if response.status_code == 200 and done(last):
            return last
        time.sleep(1)
    pytest.fail(f"Tiempo agotado esperando {url}; última respuesta: {last}")
