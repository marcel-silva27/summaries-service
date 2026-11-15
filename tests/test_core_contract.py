"""Contrato con el chat base (lado consumidor): lo que este servicio espera de users, channels y messages.

`tests/contracts/` guarda respuestas REALES del núcleo (ver `_meta` en core_http.json). Aquí se comprueba
que el servicio las entiende, sin levantar el núcleo ni tocar sus bases. Que las fixtures sigan siendo
fieles al núcleo lo verifica `tests_integration/test_core_contract.py` (lado proveedor).
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from app.clients.core_client import CoreClient
from app.errors import ChannelNotFound, UserNotFound
from app.events import EventBroker

CONTRACTS = Path(__file__).parent / "contracts"
HTTP = json.loads((CONTRACTS / "core_http.json").read_text(encoding="utf-8"))
IDS = HTTP["_meta"]["ids"]
INTERACTIONS = HTTP["interactions"]


@pytest.fixture
def core():
    """CoreClient cuyo núcleo responde exactamente lo grabado; una petición no grabada hace fallar la prueba."""
    served = []

    def handler(request):
        for name, interaction in INTERACTIONS.items():
            expected = interaction["request"]
            if request.method == expected["method"] and request.url.path == expected["path"]:
                served.append(name)
                return httpx.Response(interaction["response"]["status"], json=interaction["response"]["body"])
        pytest.fail(f"Petición fuera del contrato: {request.method} {request.url.path}")

    with CoreClient(users_url="http://users", channels_url="http://channels", messages_url="http://messages",
                    transport=httpx.MockTransport(handler)) as client:
        client.served = served
        yield client


def test_get_user_reads_the_recorded_user(core):
    user = core.get_user(IDS["user"])
    assert user["id"] == IDS["user"] and user["display_name"] == "Ana"
    assert core.served == ["get_user"]


def test_get_channel_reads_the_recorded_channel(core):
    channel = core.get_channel(IDS["channel"])
    assert channel["id"] == IDS["channel"] and channel["name"] == "General"
    assert core.served == ["get_channel"]


def test_list_messages_reads_the_recorded_history(core):
    (message,) = core.list_messages(IDS["channel"])
    recorded = INTERACTIONS["list_messages"]["response"]["body"][0]
    assert message["id"] == recorded["id"] and message["author_id"] == recorded["author_id"]
    assert message["content"] == recorded["content"]


def test_recorded_404s_map_to_the_services_own_errors(core):
    with pytest.raises(UserNotFound):
        core.get_user(IDS["unknown"])
    with pytest.raises(ChannelNotFound):
        core.get_channel(IDS["unknown"])


def test_an_unknown_channel_history_is_empty_not_an_error(core):
    # Particularidad del núcleo grabada en el contrato: `messages` responde 200 [] aunque el canal no exista.
    assert core.list_messages(IDS["unknown"]) == []


def test_recorded_message_event_is_consumed_into_the_local_history(repository):
    event = json.loads((CONTRACTS / "chat.message.created.json").read_text(encoding="utf-8"))
    channel = MagicMock()
    EventBroker(repository)._consume_history(channel, SimpleNamespace(delivery_tag=1, routing_key=event["type"]),
                                             None, json.dumps(event).encode())
    channel.basic_ack.assert_called_once_with(delivery_tag=1)
    channel.basic_reject.assert_not_called()
    (stored,) = repository.list_messages(IDS["channel"])
    assert stored.id == IDS["message"] and stored.content == event["data"]["content"]


def test_the_event_payload_and_the_http_message_have_the_same_shape():
    event = json.loads((CONTRACTS / "chat.message.created.json").read_text(encoding="utf-8"))
    assert event["data"] == INTERACTIONS["list_messages"]["response"]["body"][0]
    assert event["subject_id"] == event["data"]["id"]
