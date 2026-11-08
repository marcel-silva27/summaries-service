"""Gateway de resúmenes con HTTP simulado, sin llamar al chat ni a Gemini."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.main import create_app


NOW = "2026-11-07T12:00:00Z"
SUMMARY = {"id": "s1", "channel_id": "c1", "messageState": "pendiente",
           "created_at": NOW, "updated_at": NOW}


@pytest.fixture
def gateway_client(monkeypatch):
    monkeypatch.setenv("SUMMARIES_URL", "http://summaries:8000")

    def client(handler):
        return TestClient(create_app(transport=httpx.MockTransport(handler)), raise_server_exceptions=False)
    return client


def execute(client, query, variables=None):
    response = client.post("/api/v1/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200
    return response.json()


def test_request_and_poll_a_summary(gateway_client):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "summaries"
        if request.method == "POST":
            assert request.url.path == "/api/v1/channels/c1/summaries"
            assert json.loads(request.content) == {"user_id": "u1", "timeInterval": None}
            return httpx.Response(202, json={"summary_id": "s1", "messageState": "pendiente"})
        assert request.url.path == "/api/v1/summaries/s1"
        if len(calls) == 2:
            return httpx.Response(200, json=SUMMARY)
        return httpx.Response(200, json=SUMMARY | {"messageState": "completada", "content": "Entregar el viernes.",
            "structured": {"overview": "Entregar el viernes.", "decisions": ["Entregar el viernes"],
                           "events": [], "tasks": []}})

    with gateway_client(handler) as client:
        accepted = execute(client, 'mutation { requestSummary(channel_id: "c1", input: {user_id: "u1"}) '
                                   '{ summary_id messageState } }')
        assert accepted == {"data": {"requestSummary": {"summary_id": "s1", "messageState": "pendiente"}}}
        query = '{ getSummary(id: "s1") { messageState content structured { overview decisions } } }'
        assert execute(client, query)["data"]["getSummary"]["messageState"] == "pendiente"
        result = execute(client, query)["data"]["getSummary"]
    assert result == {"messageState": "completada", "content": "Entregar el viernes.",
                      "structured": {"overview": "Entregar el viernes.", "decisions": ["Entregar el viernes"]}}
    assert len(calls) == 3


@pytest.mark.parametrize("field,path,payload", [
    ('getSummary(id: "s1")', "/summaries/s1", SUMMARY),
    ('listSummaries(channel_id: "c1", limit: 2, offset: 3)', "/channels/c1/summaries", [SUMMARY]),
    ('getLatestSummary(channel_id: "c1")', "/channels/c1/summaries/latest", SUMMARY),
])
def test_summary_queries(gateway_client, field, path, payload):
    def handler(request):
        assert request.method == "GET" and request.url.host == "summaries"
        assert request.url.path == "/api/v1" + path
        if field.startswith("listSummaries"):
            assert dict(request.url.params) == {"limit": "2", "offset": "3"}
        return httpx.Response(200, json=payload)

    with gateway_client(handler) as client:
        assert "errors" not in execute(client, "{ " + field + " { id } }")


@pytest.mark.parametrize("failure,status", [("timeout", 504), ("network", 503), ("invalid_json", 502),
                                           ("invalid_body", 502), (404, 404), (409, 409), (422, 422), (500, 502), (302, 502)])
def test_failures_preserve_other_fields_and_hide_internal_details(gateway_client, failure, status):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/v1/summaries/s1":
            return httpx.Response(200, json=SUMMARY)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-internal-detail")
        if failure == "network":
            raise httpx.ConnectError("secret-internal-detail")
        if failure == "invalid_json":
            return httpx.Response(200, content=b"secret-internal-detail")
        if failure == "invalid_body":
            return httpx.Response(200, json={"secret-internal-detail": "invalid"})
        return httpx.Response(failure, json={"detail": "secret-internal-detail"}, headers={"Location": "http://other"})

    with gateway_client(handler) as client:
        result = execute(client, '{ getSummary(id: "s1") { id } getLatestSummary(channel_id: "c1") { id } }')
    assert result["data"] == {"getSummary": {"id": "s1"}, "getLatestSummary": None}
    assert result["errors"][0]["extensions"]["status"] == status
    assert result["errors"][0]["extensions"]["service"] == "summaries"
    assert "detail" in result["errors"][0]["extensions"]
    assert "secret-internal-detail" not in str(result)
    assert len(calls) == 2


@pytest.mark.parametrize("query", [
    'mutation { requestSummary(channel_id: "c1", input: {user_id: ""}) { summary_id } }',
    '{ listSummaries(channel_id: "c1", limit: 0) { id } }',
    '{ listSummaries(channel_id: "c1", offset: -1) { id } }',
    'mutation { requestSummary(channel_id: "c1", input: {timeInterval: {start: "bad", end: "bad"}}) { summary_id } }',
])
def test_invalid_inputs_do_not_call_services(gateway_client, query):
    def handler(_):
        pytest.fail("Una entrada inválida no debe consultar servicios")

    with gateway_client(handler) as client:
        result = execute(client, query)
    assert result["errors"][0]["extensions"]["status"] == 422


@pytest.mark.parametrize("input_value,expected", [
    (None, {"user_id": None, "timeInterval": None}),
    ({"user_id": "u1", "timeInterval": {"start": "2026-11-07T09:00:00-03:00", "end": "2026-11-07T10:00:00-03:00"}},
     {"user_id": "u1", "timeInterval": {"start": "2026-11-07T12:00:00Z", "end": "2026-11-07T13:00:00Z"}}),
])
def test_optional_input_and_utc_interval(gateway_client, input_value, expected):
    def handler(request):
        assert json.loads(request.content) == expected
        return httpx.Response(202, json={"summary_id": "s1", "messageState": "pendiente"})

    with gateway_client(handler) as client:
        result = execute(client, 'mutation($input: SummaryInput) { requestSummary(channel_id: "c1", input: $input) '
                                 '{ summary_id } }', {"input": input_value})
    assert result == {"data": {"requestSummary": {"summary_id": "s1"}}}


def test_resource_ids_cannot_change_the_service_host_or_query(gateway_client):
    resource = "../other/?secret=value#fragment"

    def handler(request):
        assert request.url.host == "summaries" and not request.url.query and not request.url.fragment
        assert b"%2E%2E%2Fother%2F%3Fsecret%3Dvalue%23fragment" in request.url.raw_path
        return httpx.Response(200, json=SUMMARY | {"id": resource})

    with gateway_client(handler) as client:
        assert "errors" not in execute(client, "query($id: ID!) { getSummary(id: $id) { id } }", {"id": resource})


def test_health_explorer_and_cors_work_without_services(gateway_client):
    def handler(_):
        pytest.fail("No se deben consultar servicios")

    with gateway_client(handler) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/api/v1/graphql", headers={"Accept": "text/html"}).status_code == 200
        response = client.options("/api/v1/graphql", headers={"Origin": "http://localhost:5173",
                                  "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Content-Type"})
        assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"


def test_failed_writes_are_not_retried(gateway_client):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, text="secret-internal-detail")

    with gateway_client(handler) as client:
        result = execute(client, 'mutation { requestSummary(channel_id: "c1") { summary_id } }')
    assert result["errors"][0]["extensions"]["status"] == 502
    assert len(calls) == 1 and "secret-internal-detail" not in str(result)


def test_unexpected_errors_are_generic(gateway_client, monkeypatch):
    import gateway.main

    async def broken(*args, **kwargs):
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr(gateway.main, "fetch", broken)
    with gateway_client(lambda _: httpx.Response(200)) as client:
        result = execute(client, '{ getSummary(id: "s1") { id } }')
    assert result["errors"][0]["extensions"] == {"detail": "Error interno", "status": 500}
    assert "secret-internal-detail" not in str(result)
