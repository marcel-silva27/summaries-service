"""Contrato con el chat base (lado proveedor): las fixtures de tests/contracts/ siguen siendo fieles al núcleo real.

Si `users`, `channels` o `messages` cambian una ruta, un campo o un tipo, estas pruebas fallan antes de que
el cambio llegue al consumidor (este servicio). Los valores variables (IDs, fechas) se comparan por su forma.
"""
import json
import os
import time
from pathlib import Path

import pika
import pytest
from conftest import URLS, send_messages

CONTRACTS = Path(__file__).parent.parent / "tests" / "contracts"
HTTP = json.loads((CONTRACTS / "core_http.json").read_text(encoding="utf-8"))
EVENT = json.loads((CONTRACTS / "chat.message.created.json").read_text(encoding="utf-8"))


def same_shape(actual, expected, path="$"):
    """Mismas claves y tipos (recursivo); el valor exacto de IDs, fechas y textos puede variar."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: se esperaba un objeto y llegó {type(actual).__name__}"
        assert set(actual) == set(expected), f"{path}: claves {sorted(actual)} != {sorted(expected)}"
        for key in expected:
            same_shape(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: se esperaba una lista"
        if expected:
            assert actual, f"{path}: se esperaba al menos un elemento"
            same_shape(actual[0], expected[0], f"{path}[0]")
    else:
        assert type(actual) is type(expected), f"{path}: {type(actual).__name__} != {type(expected).__name__}"


@pytest.fixture
def real_ids(http, chat):
    """IDs reales que reemplazan a los de ejemplo de la fixture (el 'desconocido' sigue sin existir)."""
    send_messages(http, chat, ["Decidimos lanzar el viernes"])
    message = http.get(f"{URLS['messages']}/api/v1/channels/{chat['channel']['id']}/messages").json()[0]
    ids = HTTP["_meta"]["ids"]
    return {ids["user"]: chat["user"]["id"], ids["channel"]: chat["channel"]["id"], ids["message"]: message["id"]}


@pytest.mark.parametrize("name", HTTP["interactions"])
def test_the_real_core_answers_like_the_recorded_interaction(http, real_ids, name):
    request = HTTP["interactions"][name]["request"]
    expected = HTTP["interactions"][name]["response"]
    path = request["path"]
    for example, real in real_ids.items():
        path = path.replace(example, real)
    service = "messages" if path.endswith("/messages") else path.split("/")[3]  # /api/v1/<users|channels>/...
    response = http.request(request["method"], f"{URLS[service]}{path}")
    assert response.status_code == expected["status"]
    same_shape(response.json(), expected["body"])


def test_the_real_core_publishes_the_recorded_event_on_the_broker(http, chat):
    """Se lee del broker (no del endpoint /events): lo mismo que recibe este servicio."""
    connection = pika.BlockingConnection(pika.URLParameters(os.environ["BROKER_URL"]))
    try:
        broker = connection.channel()
        # El núcleo del curso declara `chat` como topic no durable; se declara igual, sin cambiar nada.
        broker.exchange_declare(exchange="chat", exchange_type="topic")
        queue = broker.queue_declare(queue="", exclusive=True).method.queue
        broker.queue_bind(queue=queue, exchange="chat", routing_key="chat.message.created")
        send_messages(http, chat, ["Decidimos lanzar el viernes"])
        body = None
        for _ in range(20):
            _, _, body = broker.basic_get(queue=queue, auto_ack=True)
            if body:
                break
            time.sleep(0.5)
        assert body, "El núcleo no publicó chat.message.created en el broker"
    finally:
        connection.close()
    event = json.loads(body)
    assert event["type"] == "chat.message.created"
    same_shape(event, EVENT)
    assert event["data"]["channel_id"] == chat["channel"]["id"]
