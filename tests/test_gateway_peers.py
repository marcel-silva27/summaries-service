"""Contratos de los equipos, archivos y aislamiento de fallas en el gateway."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.events import EventBroker, make_event
from gateway.main import create_app


NOW = "2026-11-07T12:00:00Z"
FILE = {"id": "f1", "channel_id": "c1", "category": "DOCUMENT", "uploader_id": "u1",
        "filename": "reporte.txt", "mime_type": "text/plain", "size_bytes": 4, "created_at": NOW}
POLL = {"poll_id": "p1", "channel_id": "c1", "author_id": "u1", "question": "Cuando?",
        "options": [{"option_id": "o1", "position": 0, "text": "Hoy", "vote_count": 0}],
        "status": "open", "duration_minutes": 5, "closes_at": NOW, "created_at": NOW, "total_votes": 0}


@pytest.fixture
def client_factory(monkeypatch):
    for name in ("users", "channels", "messages", "summaries", "search", "media", "polls"):
        monkeypatch.setenv(name.upper() + "_URL", "http://" + name)
    return lambda handler: TestClient(create_app(transport=httpx.MockTransport(handler)))


def graphql(client, query, variables=None):
    response = client.post("/api/v1/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200
    return response.json()


def test_chat_mutations_and_partial_failures(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "channels":
            return httpx.Response(200, json=[{"id": "c1", "name": "Equipo", "_id": "interno"}])
        if request.url.host == "users":
            assert json.loads(request.content) == {"display_name": "Ana"}
            return httpx.Response(201, json={"id": "u1", "display_name": "Ana"})
        return httpx.Response(503, text="private-internal-detail")

    with client_factory(handler) as client:
        result = graphql(client, '{ listChannels { id name } getPoll(id: "p1") { question } }')
        assert result["data"] == {"listChannels": [{"id": "c1", "name": "Equipo"}], "getPoll": None}
        assert result["errors"][0]["extensions"]["service"] == "polls"
        assert "private-internal-detail" not in str(result)
        assert graphql(client, 'mutation { createUser(input: {display_name: "Ana"}) { id } }') == {"data": {"createUser": {"id": "u1"}}}
    assert len(calls) == 3


def test_search_preserves_repeated_filters_and_handles_warming_index(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "search" and request.url.path == "/api/v1/channels/c1/messages/search"
        assert request.url.params.get_list("author_id") == ["u1", "u2"]
        assert request.url.params["min_date"] == "2026-11-07T12:00:00Z"
        if len(calls) == 1:
            return httpx.Response(202, json={"code": 110000, "retry_after": 2, "message": "Index not yet available"})
        return httpx.Response(200, json={"total_results": 1, "messages": [{"id": "m1", "channel_id": "c1",
            "author_id": "u1", "content": "Informe", "created_at": NOW, "highlights": ["**Informe**"],
            "indexed_at": "interno", "version": 1}]})

    query = 'query($input: SearchInput) { searchMessages(channel_id: "c1", input: $input) { indexing retry_after total_results messages { id highlights } } }'
    variables = {"input": {"author_id": ["u1", "u2"], "min_date": "2026-11-07T09:00:00-03:00", "has": ["link"]}}
    with client_factory(handler) as client:
        pending = graphql(client, query, variables)["data"]["searchMessages"]
        assert pending == {"indexing": True, "retry_after": 2, "total_results": 0, "messages": []}
        found = graphql(client, query, variables)["data"]["searchMessages"]
        assert found["total_results"] == 1 and found["messages"] == [{"id": "m1", "highlights": ["**Informe**"]}]


def test_media_gallery_forwards_identity_without_exposing_internal_fields(client_factory):
    def handler(request):
        assert request.url.host == "media" and request.headers["X-User-Id"] == "u1"
        assert "user_id" not in request.url.params
        assert request.url.params["category"] == "DOCUMENT"
        return httpx.Response(200, json={"items": [FILE | {"_id": "interno", "outbox": [{"secret": True}]}], "limit": 20, "offset": 0})

    with client_factory(handler) as client:
        result = graphql(client, '{ listAttachments(channel_id: "c1", user_id: "u1", input: {category: "DOCUMENT"}) { items { id filename } } }')
    assert result == {"data": {"listAttachments": {"items": [{"id": "f1", "filename": "reporte.txt"}]}}}


def test_file_transfer_and_delete_stay_on_gateway(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "media" and request.headers["X-User-Id"] == "u1"
        if request.method == "POST":
            assert request.url.path == "/api/v1/channels/c1/attachments"
            assert b'name="file"' in request.content and b"hola" in request.content
            return httpx.Response(201, json=FILE)
        if request.method == "DELETE":
            assert request.url.path == "/api/v1/attachments/f1"
            return httpx.Response(204)
        assert request.url.path == "/api/v1/attachments/f1/download"
        return httpx.Response(200, content=b"hola", headers={"content-type": "text/plain", "content-disposition": 'attachment; filename="reporte.txt"', "set-cookie": "private=value"})

    headers = {"X-User-Id": "u1"}
    with client_factory(handler) as client:
        uploaded = client.post("/api/v1/media/channels/c1/attachments", headers=headers,
                               files={"file": ("reporte.txt", b"hola", "text/plain")})
        assert uploaded.status_code == 201 and uploaded.json()["id"] == "f1"
        download = client.get("/api/v1/media/attachments/f1/download", headers=headers)
        assert download.content == b"hola" and download.headers["X-Content-Type-Options"] == "nosniff"
        assert "set-cookie" not in download.headers and "attachment" in download.headers["Content-Disposition"]
        assert client.delete("/api/v1/media/attachments/f1", headers=headers).status_code == 204
    assert len(calls) == 3


def test_media_errors_have_uniform_public_http_shape(client_factory):
    with client_factory(lambda _: httpx.Response(403, json={"detail": "private-internal-detail"})) as client:
        response = client.delete("/api/v1/media/attachments/f1", headers={"X-User-Id": "u1"})
        assert response.status_code == 403 and set(response.json()) == {"detail"}
        assert "private-internal-detail" not in response.text
        assert client.get("/api/v1/media/attachments/f1/download").status_code == 422


def test_empty_upload_is_rejected_before_contacting_media(client_factory):
    def handler(_):
        pytest.fail("No se debe reenviar un archivo vacío")

    with client_factory(handler) as client:
        response = client.post("/api/v1/media/channels/c1/attachments", headers={"X-User-Id": "u1"},
                               files={"file": ("vacio.txt", b"", "text/plain")})
    assert response.status_code == 422 and "detail" in response.json()


def test_poll_creation_vote_and_close_preserve_peer_contract(client_factory):
    bodies = []

    def handler(request):
        assert request.url.host == "polls"
        body = json.loads(request.content)
        bodies.append(body)
        if request.url.path == "/api/v1/polls":
            assert request.headers["Idempotency-Key"] == "create-1"
            return httpx.Response(202, json=POLL | {"status": "pending_announcement"})
        if request.url.path.endswith("/votes"):
            return httpx.Response(201, json={"poll_id": "p1", **body, "cast_at": NOW})
        return httpx.Response(200, json=POLL | {"status": "closed"})

    with client_factory(handler) as client:
        created = graphql(client, 'mutation { createPoll(input: {channel_id: "c1", author_id: "u1", question: "Cuando?", options: ["Hoy", "Manana"]}, idempotency_key: "create-1") { status } }')
        assert created["data"]["createPoll"]["status"] == "pending_announcement"
        assert "errors" not in graphql(client, 'mutation { votePoll(id: "p1", input: {voter_id: "u2", option_id: "o1"}) { poll_id } }')
        assert graphql(client, 'mutation { closePoll(id: "p1", input: {author_id: "u1"}) { status } }')["data"]["closePoll"]["status"] == "closed"
    assert bodies[0]["duration_minutes"] == 5
    assert bodies[1:] == [{"voter_id": "u2", "option_id": "o1"}, {"author_id": "u1"}]


def test_frontend_is_served_by_the_gateway(client_factory):
    with client_factory(lambda _: pytest.fail("Servir la web no consulta servicios")) as client:
        response = client.get("/")
        assert response.status_code == 200 and '<html lang="es">' in response.text
        assert 'src="/app.js"' in response.text
        assert client.get("/styles.css").status_code == 200
        assert client.get("/app.js").status_code == 200


def test_new_core_event_metadata_is_normalized(repository):
    from test_repository import message

    original = message()
    payload = make_event("chat.message.created", original.id,
                         original.model_dump(mode="json") | {"edited_at": None, "version": 1, "outbox": []})
    payload["event_id"] = "external-event-id"
    channel = MagicMock()
    runtime = EventBroker(repository)
    runtime._consume_history(channel, SimpleNamespace(delivery_tag=1, routing_key="chat.message.created"),
                             None, json.dumps(payload).encode())
    channel.basic_ack.assert_called_once_with(delivery_tag=1)
    channel.basic_reject.assert_not_called()
    assert repository.list_messages(original.channel_id)[0].id == original.id
