from unittest.mock import MagicMock

import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure

from app import config
from app.clients import core_client
from app.clients.core_client import CoreClient, CoreClientDependency
from app.errors import (
    ChannelNotFound, DependencyFailed, DependencyUnavailable, PersistenceConflict,
    SummaryNotFound, UserNotFound, register_handlers,
)
from app.main import app
from app.repository import RepositoryDependency


def test_lifespan_injects_repository_and_preserves_existing_api(repository, mongo_client, monkeypatch):
    import app.main as main

    def client_factory(*args, **kwargs):
        return MongoClient(mongo_client.address[0], mongo_client.address[1], **kwargs)

    monkeypatch.setattr(main, "MongoClient", client_factory)
    monkeypatch.setattr(main, "EventBroker", MagicMock())
    monkeypatch.setattr(main, "CoreClient", lambda: _core(
        lambda request: httpx.Response(200, json={"id": "channel", "name": "General"})))
    monkeypatch.setattr(config, "MONGODB_DATABASE", repository.database.name)
    with TestClient(app) as client:
        assert app.state.repository.database.name == repository.database.name
        assert isinstance(app.state.core_client, CoreClient)
        http_client = app.state.core_client._http
        assert not http_client.is_closed
        assert app.state.repository.summaries.index_information()["summary_id_unique"]["unique"]
        assert client.get("/health").json() == {"status": "ok"}
        assert isinstance(client.get("/api/v1/events").json(), list)
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        for path in ("/api/v1/summaries/{summary_id}", "/api/v1/channels/{channel_id}/summaries",
                     "/api/v1/channels/{channel_id}/summaries/latest"):
            assert path in schema["paths"]
        assert client.post("/api/v1/channels/channel/summaries").status_code == 202
        events = client.get("/api/v1/events", params={"limit": 5}).json()  # Durables: vienen de Mongo.
        assert [e["type"] for e in events] == ["summary.requested"]
        assert client.get("/api/v1/events", params={"limit": 0}).status_code == 422
        assert client.get("/api/v1/channels/channel/summaries").status_code == 200
        assert client.get("/api/v1/channels/channel/summaries/latest").status_code == 404
        assert client.get("/api/v1/summaries/summary").status_code == 404
    assert http_client.is_closed


@pytest.mark.parametrize("failure", ["ping", "indexes"])
def test_failed_startup_closes_client_and_does_not_start_serving(monkeypatch, failure):
    import app.main as main

    mongo = MagicMock()
    if failure == "ping":
        mongo.admin.command.side_effect = ConnectionFailure("unreachable")
    else:
        monkeypatch.setattr(main.MongoRepository, "ensure_indexes", MagicMock(side_effect=OperationFailure("bad indexes")))
    monkeypatch.setattr(main, "MongoClient", MagicMock(return_value=mongo))
    with pytest.raises((ConnectionFailure, OperationFailure)):
        with TestClient(app):
            pytest.fail("An application without storage must not start")
    mongo.close.assert_called_once()


def test_dependency_and_error_contracts(repository):
    test_app = FastAPI()
    test_app.state.repository = repository
    register_handlers(test_app)

    @test_app.get("/missing")
    def missing(storage: RepositoryDependency):
        return storage.get_summary("missing")

    @test_app.get("/conflict")
    def conflict():
        raise PersistenceConflict("Conflict")

    @test_app.get("/unavailable")
    def unavailable():
        raise ConnectionFailure("private connection details")

    with TestClient(test_app) as client:
        result = client.get("/missing")
        assert result.status_code == 404
        assert result.json() == {"detail": SummaryNotFound.detail}
        assert client.get("/conflict").status_code == 409
        result = client.get("/unavailable")
        assert result.status_code == 503
        assert result.json() == {"detail": "Summary storage unavailable"}


def _core(handler, **kwargs):
    return CoreClient(users_url="http://users:8000/", channels_url="http://channels:8000/",
                      messages_url="http://messages:8000/", transport=httpx.MockTransport(handler), **kwargs)


def _message(id="message", **values):
    return dict(id=id, channel_id="channel", author_id="user", content="Decidimos entregar el viernes.",
                created_at="2026-10-18T12:00:00.123456-03:00") | values


@pytest.mark.parametrize("method,resource,payload,host,path", [
    ("get_channel", "channel", {"id": "channel", "name": "General"}, "channels", "/api/v1/channels/channel"),
    ("get_user", "user", {"id": "user", "display_name": "Ana"}, "users", "/api/v1/users/user"),
    ("list_messages", "channel", [_message()], "messages", "/api/v1/channels/channel/messages"),
])
def test_core_uses_verified_routes_and_configured_timeout(method, resource, payload, host, path):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == host
        assert request.url.path == path
        assert request.headers["Accept"] == "application/json"
        assert set(request.extensions["timeout"].values()) == {1.5}
        return httpx.Response(200, json=payload)

    with _core(handler, timeout_seconds=1.5) as client:
        result = getattr(client, method)(resource)
    assert len(calls) == 1
    if method == "list_messages":
        assert result[0]["created_at"] == "2026-10-18T15:00:00.123000Z"
        assert result[0]["id"] == "message"
    else:
        assert result == payload


@pytest.mark.parametrize("method,error", [
    ("get_channel", ChannelNotFound), ("get_user", UserNotFound), ("list_messages", ChannelNotFound),
])
def test_core_maps_missing_resources(method, error):
    with _core(lambda _: httpx.Response(404, text="private upstream body")) as client:
        with pytest.raises(error):
            getattr(client, method)("missing")


@pytest.mark.parametrize("status,error", [
    (429, DependencyUnavailable), (503, DependencyUnavailable), (504, DependencyUnavailable),
    (500, DependencyFailed), (502, DependencyFailed), (400, DependencyFailed),
    (401, DependencyFailed), (403, DependencyFailed), (422, DependencyFailed),
    (204, DependencyFailed), (206, DependencyFailed), (302, DependencyFailed),
])
def test_core_maps_other_statuses_without_forwarding_bodies_or_following_redirects(status, error):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private upstream body", headers={"Location": "http://elsewhere/private"})

    with _core(handler) as client:
        with pytest.raises(error) as caught:
            client.get_channel("channel")
    assert "private" not in caught.value.detail
    assert len(calls) == 1


@pytest.mark.parametrize("exception,error", [
    (httpx.ConnectTimeout, DependencyUnavailable), (httpx.ReadTimeout, DependencyUnavailable),
    (httpx.WriteTimeout, DependencyUnavailable), (httpx.PoolTimeout, DependencyUnavailable),
    (httpx.ConnectError, DependencyUnavailable), (httpx.ReadError, DependencyUnavailable),
    (httpx.WriteError, DependencyUnavailable), (httpx.ProxyError, DependencyUnavailable),
    (httpx.RemoteProtocolError, DependencyFailed), (httpx.DecodingError, DependencyFailed),
])
def test_core_maps_transport_failures_without_automatic_retries(exception, error):
    calls = []

    def handler(request):
        calls.append(request)
        raise exception("private connection details", request=request)

    with _core(handler) as client:
        with pytest.raises(error) as caught:
            client.get_user("user")
    assert "private" not in caught.value.detail
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [None, [], {}, {"id": "channel"},
    {"id": "other", "name": "General"}, {"id": "channel", "name": 42},
    {"id": "channel", "name": ""}, {"id": "channel", "name": "x" * 81},
])
def test_core_rejects_invalid_channel_responses(payload):
    with _core(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(DependencyFailed):
            client.get_channel("channel")


@pytest.mark.parametrize("payload", [None, {}, {"id": "user"},
    {"id": "other", "display_name": "Ana"}, {"id": "user", "display_name": 42},
])
def test_core_rejects_invalid_user_responses(payload):
    with _core(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(DependencyFailed):
            client.get_user("user")


@pytest.mark.parametrize("payload", [None, {}, "text", [None], [_message(channel_id="other")],
    [_message(author_id="")], [_message(content="x" * 4001)], [_message(created_at="invalid")],
    [_message(created_at="2026-10-18T12:00:00")], [_message(created_at=1792335600)],
    [_message(), {"id": "incomplete"}],
])
def test_core_rejects_invalid_message_lists_as_a_whole(payload):
    with _core(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(DependencyFailed):
            client.list_messages("channel")


def test_core_rejects_malformed_json():
    with _core(lambda _: httpx.Response(200, content=b"not json")) as client:
        with pytest.raises(DependencyFailed):
            client.get_channel("channel")


def test_core_empty_history_is_not_channel_validation():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    with _core(handler) as client:
        assert client.list_messages("unknown-channel") == []
    assert len(calls) == 1
    assert calls[0].url.host == "messages"


def test_core_returns_all_messages_in_upstream_order_and_ignores_extra_fields(repository):
    payload = [_message(f"m{index}", extra="future field") for index in reversed(range(1001))]
    with _core(lambda _: httpx.Response(200, json=payload)) as client:
        messages = client.list_messages("channel")
    assert [item["id"] for item in messages] == [item["id"] for item in payload]
    assert "extra" not in messages[0]
    from app.models import LocalMessage
    assert repository.upsert_message(LocalMessage(**messages[0]))


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_core_rejects_unbounded_or_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        _core(lambda _: httpx.Response(200), timeout_seconds=timeout)


@pytest.mark.parametrize("url", ["", "ftp://users", "users:8000", "http://", "http://users?x=1", "http://users/#fragment"])
def test_core_rejects_invalid_base_urls(url):
    with pytest.raises(ValueError):
        CoreClient(users_url=url)


def test_core_encodes_resource_id_without_changing_host_or_query():
    resource = "user/?x=#fragment"

    def handler(request):
        assert request.url.host == "users"
        assert not request.url.query
        assert not request.url.fragment
        assert b"user%2F%3Fx%3D%23fragment" in request.url.raw_path
        return httpx.Response(200, json={"id": resource, "display_name": "Ana"})

    with _core(handler) as client:
        assert client.get_user(resource)["id"] == resource


def test_core_context_closes_connections_even_on_failure():
    client = _core(lambda _: httpx.Response(500))
    with pytest.raises(DependencyFailed):
        with client:
            client.get_user("user")
    assert client._http.is_closed


def test_core_dependency_preserves_http_error_contract():
    test_app = FastAPI()
    register_handlers(test_app)

    @test_app.get("/users/{user_id}")
    def user(user_id: str, core: CoreClientDependency):
        return core.get_user(user_id)

    with _core(lambda _: httpx.Response(404)) as core:
        test_app.state.core_client = core
        with TestClient(test_app) as client:
            result = client.get("/users/missing")
    assert result.status_code == 422
    assert result.json() == {"detail": UserNotFound.detail}


@pytest.mark.parametrize("function,resource,payload", [
    ("get_channel", "channel", {"id": "channel", "name": "General"}),
    ("get_user", "user", {"id": "user", "display_name": "Ana"}),
    ("list_messages", "channel", []),
])
def test_core_original_functions_remain_usable_and_close_their_clients(monkeypatch, function, resource, payload):
    client = _core(lambda _: httpx.Response(200, json=payload))
    monkeypatch.setattr(core_client, "CoreClient", lambda: client)
    assert getattr(core_client, function)(resource) == payload
    assert client._http.is_closed


def test_post_is_accepted_durably_without_a_running_broker_or_llm(api, repository):
    result = api.post("/api/v1/channels/channel/summaries", json={"user_id": "user"})
    assert result.status_code == 202
    assert result.json()["messageState"] == "pendiente"
    summary_id = result.json()["summary_id"]
    assert result.headers["Location"] == f"/api/v1/summaries/{summary_id}"
    polled = api.get(result.headers["Location"])
    assert polled.status_code == 200
    assert polled.json()["requested_by"] == "user"
    assert "_request_delivery" not in polled.json()
    event = repository.pending_request_events()[0]
    assert event["subject_id"] == summary_id
    assert event["type"] == "summary.requested"
    assert event["data"] == {"channel_id": "channel", "requested_by": "user", "timeInterval": None}
    assert repository.received_requests() == []


@pytest.mark.parametrize("body", [None, {}, {"timeInterval": {
    "start": "2026-10-18T12:00:00-03:00", "end": "2026-10-18T13:00:00-03:00"}}])
def test_post_accepts_optional_body_and_normalizes_interval(api, repository, body):
    response = api.post("/api/v1/channels/channel/summaries", json=body)
    assert response.status_code == 202
    summary = repository.get_summary(response.json()["summary_id"])
    assert summary.requested_by is None
    if body and "timeInterval" in body:
        assert summary.timeInterval.start.hour == 15


@pytest.mark.parametrize("channel,body,status", [
    ("missing", {}, 404), ("channel", {"user_id": "missing"}, 422),
    ("unavailable", {}, 503), ("channel", {"user_id": "unavailable"}, 503),
    ("channel", {"timeInterval": {"start": "bad", "end": "bad"}}, 422),
    ("channel", {"unknown": True}, 422),
])
def test_failed_request_validation_creates_no_summary_or_event(api, repository, channel, body, status):
    assert api.post(f"/api/v1/channels/{channel}/summaries", json=body).status_code == status
    assert repository.summaries.count_documents({}) == 0
    assert repository.pending_request_events() == []


def test_read_routes_filter_paginate_and_return_latest_completed(api, repository):
    from test_repository import completed

    done = completed(repository)
    pending = repository.create_summary("channel")
    repository.create_summary("other")
    result = api.get("/api/v1/channels/channel/summaries")
    assert result.status_code == 200
    assert {item["id"] for item in result.json()} == {done.id, pending.id}
    assert api.get("/api/v1/channels/channel/summaries?limit=1&offset=1").json() == result.json()[1:2]
    assert api.get("/api/v1/channels/channel/summaries/latest").json()["id"] == done.id
    assert api.get("/api/v1/channels/empty/summaries").json() == []
    assert api.get("/api/v1/channels/empty/summaries/latest").status_code == 404
    assert api.get("/api/v1/channels/missing/summaries").status_code == 404
    assert api.get("/api/v1/channels/missing/summaries/latest").status_code == 404


@pytest.mark.parametrize("query", ["limit=0", "limit=1001", "offset=-1", "limit=not-an-int"])
def test_invalid_list_pagination_is_rejected(api, query):
    assert api.get(f"/api/v1/channels/channel/summaries?{query}").status_code == 422


def test_polling_is_local_and_exposes_failure_without_calling_core(api, repository):
    summary = repository.create_summary("unavailable")
    repository.fail_summary(summary.id, "LLM timeout")
    result = api.get(f"/api/v1/summaries/{summary.id}")
    assert result.status_code == 200
    assert result.json()["messageState"] == "fallida"
    assert result.json()["error"] == "LLM timeout"
    assert api.get("/api/v1/summaries/missing").status_code == 404
