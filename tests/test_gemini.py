"""El adaptador de Gemini se prueba solo con transporte simulado: nunca se usa la cuota real."""
import json

import httpx
import pytest

from app.clients.llm_client import GeminiProvider, LLMError, LLMRequest, PROMPT

KEY = "secret-key-123"
REQUEST = LLMRequest("s1", "c1", [
    {"id": "m1", "author_id": "u1", "author_name": "Ana", "content": "Decidimos entregar el viernes",
     "created_at": "2026-10-18T10:00:00+00:00"},
    {"id": "m2", "author_id": "u2", "author_name": "Beto", "content": "Yo hago las pruebas",
     "created_at": "2026-10-18T10:05:00+00:00"}])


def ok(text="Resumen"):
    return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "pensando", "thought": True}, {"text": text, "thoughtSignature": "x"}]}}]})


def quota(delay=None):
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}] if delay else []
    return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                               "message": "quota", "details": details}})


class Server:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def models(self):
        return [r.url.path.split("/models/")[1].split(":")[0] for r in self.requests]


def provider(server, *, fallback="backup", sleeps=None, **values):
    return GeminiProvider(api_key=KEY, model="main", fallback_model=fallback, base_url="https://api.test/v1beta",
                          timeout=5, transport=httpx.MockTransport(server),
                          sleep=(sleeps.append if sleeps is not None else lambda _: None), **values)


def test_request_format_and_response_parsing_ignore_thought_parts():
    server = Server(ok("Decisión: entregar el viernes."))
    assert provider(server).generate(REQUEST) == "Decisión: entregar el viernes."
    sent = server.requests[0]
    assert sent.url == "https://api.test/v1beta/models/main:generateContent"
    assert sent.headers["x-goog-api-key"] == KEY and KEY not in str(sent.url)
    body = json.loads(sent.content)
    assert body["systemInstruction"]["parts"][0]["text"] == PROMPT
    transcript = body["contents"][0]["parts"][0]["text"]
    assert "[2026-10-18T10:00:00+00:00] Ana: Decidimos entregar el viernes" in transcript
    assert "Beto: Yo hago las pruebas" in transcript


def test_short_quota_wait_is_retried_once_on_the_same_model():
    server, sleeps = Server(quota("3.5s"), ok()), []
    assert provider(server, sleeps=sleeps).generate(REQUEST) == "Resumen"
    assert server.models == ["main", "main"] and sleeps == [3.5]


def test_long_or_unknown_quota_wait_goes_straight_to_the_fallback_model():
    server, sleeps = Server(quota("45s"), ok("del respaldo")), []
    assert provider(server, sleeps=sleeps).generate(REQUEST) == "del respaldo"
    assert server.models == ["main", "backup"] and sleeps == []
    server = Server(quota(), ok("del respaldo"))
    assert provider(server).generate(REQUEST) == "del respaldo"


def test_exhausted_quota_on_every_model_is_a_retryable_error_without_secrets():
    server = Server(quota(), quota())
    with pytest.raises(LLMError) as error:
        provider(server).generate(REQUEST)
    assert error.value.retryable and "Cuota" in error.value.reason and KEY not in error.value.reason
    assert len(server.requests) == 2


@pytest.mark.parametrize("failure", [httpx.Response(503), httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
def test_server_errors_timeouts_and_network_failures_use_the_fallback(failure):
    server = Server(failure, ok("respaldo"))
    assert provider(server).generate(REQUEST) == "respaldo"


def test_without_a_fallback_model_a_transient_error_is_surfaced_as_retryable():
    with pytest.raises(LLMError) as error:
        provider(Server(httpx.Response(500)), fallback="").generate(REQUEST)
    assert error.value.retryable


@pytest.mark.parametrize("response, text", [
    (httpx.Response(403, json={"error": {"message": "denied"}}), "Credencial"),
    (httpx.Response(400, json={"error": {"message": "bad model"}}), "bad model"),
    (httpx.Response(404, text="not json"), "HTTP 404"),
    (httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}), "bloqueó"),
    (httpx.Response(200, json={"candidates": []}), "candidatos"),
    (httpx.Response(200, json={"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "x"}]}}]}),
     "incompleta"),
    (httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "  "}]}}]}),
     "vacío"),
    (httpx.Response(200, text="<html>"), "no es JSON"),
])
def test_permanent_errors_are_not_retried_and_do_not_try_the_fallback(response, text):
    server = Server(response)
    with pytest.raises(LLMError, match=text) as error:
        provider(server).generate(REQUEST)
    assert not error.value.retryable and len(server.requests) == 1 and KEY not in error.value.reason


def test_configuration_is_validated_when_the_provider_is_built():
    with pytest.raises(ValueError):
        GeminiProvider(api_key="", model="m", base_url="https://x", timeout=1)
    with pytest.raises(ValueError):
        GeminiProvider(api_key=KEY, model=" ", base_url="https://x", timeout=1)
    same = GeminiProvider(api_key=KEY, model="m", fallback_model="m", base_url="https://x", timeout=1)
    assert same.models == ["m"]
