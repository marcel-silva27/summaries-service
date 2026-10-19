import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pika
import pytest

from app import config
from app.clients.core_client import CoreClient
from app.clients.llm_client import LLMError, LLMRequest, MockProvider, build_provider
from app.errors import SummaryStateConflict
from app.events import EventBroker, make_event
from app.models import LocalMessage, MessageState, SummaryRequest, TimeInterval, utc_now
from app.services import limit_window
from app.worker import SummaryWorker

BASE = utc_now() - timedelta(hours=2)


def msg(id, content="Hola", at=None, channel="channel", author="u1"):
    return LocalMessage(id=id, channel_id=channel, author_id=author, content=content,
                        created_at=at or BASE)


class Core:
    """Núcleo simulado: mensajes por canal, usuarios y fallos controlables."""

    def __init__(self):
        self.messages: dict[str, list[LocalMessage]] = {}
        self.users = {"u1": "Ana", "u2": "Beto"}
        self.channel_status = 200
        self.user_status = None

    def handler(self, request):
        parts = request.url.path.strip("/").split("/")
        if parts[2] == "users":
            if self.user_status:
                return httpx.Response(self.user_status)
            name = self.users.get(parts[3])
            return httpx.Response(200, json={"id": parts[3], "display_name": name}) if name else httpx.Response(404)
        if self.channel_status != 200:
            return httpx.Response(self.channel_status)
        if len(parts) == 5:
            return httpx.Response(200, json=[m.model_dump(mode="json") for m in self.messages.get(parts[3], [])])
        return httpx.Response(200, json={"id": parts[3], "name": "General"})

    def client(self):
        return CoreClient(users_url="http://users", channels_url="http://channels",
                          messages_url="http://messages", transport=httpx.MockTransport(self.handler))


class Spy(MockProvider):
    def __init__(self):
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return super().generate(request)


@pytest.fixture(autouse=True)
def fast_retries_and_default_limits(monkeypatch):
    monkeypatch.setattr(config, "WORKER_RETRY_SECONDS", 0.001)
    monkeypatch.setattr(config, "LLM_MAX_DAYS", 7)
    monkeypatch.setattr(config, "LLM_MAX_MESSAGES", 500)
    monkeypatch.setattr(config, "LLM_MAX_INPUT_CHARS", 400_000)


@pytest.fixture
def core():
    return Core()


def received(repository, core=None, *, channel="channel", **request):
    summary = repository.create_summary(channel, SummaryRequest(**request) if request else None,
                                        event_factory=lambda item: make_event("summary.requested", item.id,
                                                                              {"channel_id": channel}))
    repository.receive_summary_request(repository.summaries.find_one({"id": summary.id})["_request_delivery"]["event"])
    return summary


def seed(repository, core, items, channel="channel"):
    core.messages.setdefault(channel, []).extend(items)
    repository.upsert_messages(items)


def worker_for(repository, core, provider=None):
    return SummaryWorker(repository, core.client(), provider or Spy())


def test_incremental_request_completes_with_digest_and_advances_checkpoint(repository, core):
    seed(repository, core, [msg("m1", at=BASE), msg("m2", "Decidimos entregar el viernes", BASE + timedelta(minutes=1),
                                                    author="u2")])
    summary = received(repository, core)
    provider = Spy()
    assert worker_for(repository, core, provider).drain() == 1
    done = repository.get_summary(summary.id)
    assert done.messageState == MessageState.completada
    assert "Ana, Beto" in done.content and "2 mensajes" in done.content
    digest = repository.get_digest_for_summary(summary.id)
    assert (digest.message_count, digest.first_message_id, digest.last_message_id) == (2, "m1", "m2")
    assert digest.omitted_count == 0
    checkpoint = repository.get_checkpoint("channel")
    assert (checkpoint.last_message_id, checkpoint.messages_since) == ("m2", 0)
    assert "_coverage_pending" not in repository.summaries.find_one({"id": summary.id})
    assert "_lease" not in repository.summaries.find_one({"id": summary.id})
    assert provider.calls == 1


def test_interval_request_does_not_consume_channel_coverage(repository, core):
    items = [msg("m1", at=BASE), msg("m2", at=BASE + timedelta(minutes=1))]
    seed(repository, core, items)
    window = TimeInterval(start=BASE - timedelta(minutes=1), end=BASE + timedelta(minutes=5))
    summary = received(repository, core, timeInterval=window)
    worker_for(repository, core).drain()
    assert repository.get_summary(summary.id).messageState == MessageState.completada
    assert repository.get_checkpoint("channel").messages_since == 2


@pytest.mark.parametrize("content, reason", [
    ("[mock:fail]", "El proveedor simulado rechazó la solicitud"),
])
def test_non_retryable_provider_error_fails_with_a_readable_reason(repository, core, content, reason):
    seed(repository, core, [msg("m1", content)])
    summary = received(repository, core)
    provider = Spy()
    worker_for(repository, core, provider).drain()
    failed = repository.get_summary(summary.id)
    assert (failed.messageState, failed.error) == (MessageState.fallida, reason)
    assert provider.calls == 1
    assert repository.get_checkpoint("channel").messages_since == 1  # Nada se da por cubierto.


def test_empty_window_fails_without_calling_the_llm(repository, core):
    summary = received(repository, core)
    provider = Spy()
    worker_for(repository, core, provider).drain()
    failed = repository.get_summary(summary.id)
    assert failed.messageState == MessageState.fallida and "No hay mensajes" in failed.error
    assert provider.calls == 0


def test_retryable_error_is_retried_then_succeeds(repository, core):
    seed(repository, core, [msg("m1")])
    summary = received(repository, core)
    provider = MagicMock()
    provider.generate.side_effect = [LLMError("cuota agotada", retryable=True), "Resumen real"]
    worker_for(repository, core, provider).drain()
    assert repository.get_summary(summary.id).content == "Resumen real"
    assert provider.generate.call_count == 2


def test_retryable_error_gives_up_after_max_attempts(repository, core):
    seed(repository, core, [msg("m1", "[mock:retry]")])
    summary = received(repository, core)
    provider = Spy()
    worker_for(repository, core, provider).drain()
    failed = repository.get_summary(summary.id)
    assert failed.messageState == MessageState.fallida
    assert failed.error.endswith(f"(tras {config.WORKER_MAX_ATTEMPTS} intentos)")
    assert provider.calls == config.WORKER_MAX_ATTEMPTS


def test_unavailable_core_is_retried_but_unknown_channel_fails_at_once(repository, core):
    seed(repository, core, [msg("m1")])
    flaky = received(repository, core)
    core.channel_status = 503
    worker_for(repository, core).drain()
    assert "tras 3 intentos" in repository.get_summary(flaky.id).error
    missing = received(repository, core)
    core.channel_status = 404
    worker_for(repository, core).drain()
    assert repository.get_summary(missing.id).error == "Channel not found"


def test_author_names_fall_back_to_ids_when_the_core_cannot_resolve_them(repository, core):
    seed(repository, core, [msg("m1", author="ghost")])
    summary = received(repository, core)
    worker_for(repository, core).drain()
    assert "ghost" in repository.get_summary(summary.id).content
    core.user_status = 503
    other = msg("m2", at=BASE + timedelta(minutes=1), author="u1")
    seed(repository, core, [other])
    again = received(repository, core)
    worker = worker_for(repository, core)
    worker.drain()
    assert repository.get_summary(again.id).messageState == MessageState.completada
    assert "u1" in repository.get_summary(again.id).content and "u1" not in worker._names


def test_a_persisted_digest_is_reused_after_a_crash_without_calling_the_llm_again(repository, core):
    seed(repository, core, [msg("m1")])
    summary = received(repository, core)
    first = worker_for(repository, core, Spy())
    claim = repository.claim_summary(first.owner, 120)
    first._call_llm(claim.summary, first.service.build_window(summary.id))  # Cae antes de completar.
    repository.summaries.update_one({"id": summary.id}, {"$set": {"_lease.expires_at": BASE}})
    second_provider = Spy()
    worker_for(repository, core, second_provider).drain()
    assert second_provider.calls == 0
    done = repository.get_summary(summary.id)
    assert done.messageState == MessageState.completada
    assert done.content == repository.get_digest_for_summary(summary.id).content
    assert repository.get_checkpoint("channel").messages_since == 0


def test_a_worker_with_an_expired_lease_cannot_confirm_results(repository, core):
    seed(repository, core, [msg("m1")])
    summary = received(repository, core)
    stale = worker_for(repository, core)
    claim = repository.claim_summary(stale.owner, 120)
    stale._call_llm(claim.summary, stale.service.build_window(summary.id))
    repository.summaries.update_one({"id": summary.id}, {"$set": {"_lease.expires_at": BASE}})
    fresh = repository.claim_summary("other-worker", 120)
    assert fresh is not None and fresh.attempts == 2
    assert not repository.renew_lease(summary.id, stale.owner, 120)
    with pytest.raises(SummaryStateConflict):
        repository.complete_summary(summary.id, owner=stale.owner)
    with pytest.raises(SummaryStateConflict):
        repository.fail_summary(summary.id, "tarde", owner=stale.owner)
    assert repository.complete_summary(summary.id, owner="other-worker").messageState == MessageState.completada


def test_coverage_is_recovered_when_the_process_falls_after_completing(repository, core):
    seed(repository, core, [msg("m1"), msg("m2", at=BASE + timedelta(minutes=1))])
    summary = received(repository, core)
    worker = worker_for(repository, core)
    real = repository.advance_checkpoint
    repository.advance_checkpoint = MagicMock(side_effect=SummaryStateConflict("simulado"))
    worker.drain()
    repository.advance_checkpoint = real
    assert repository.get_summary(summary.id).messageState == MessageState.completada
    assert repository.get_checkpoint("channel").messages_since == 2
    assert repository.pending_coverage()[0].id == summary.id
    worker.drain()
    assert repository.get_checkpoint("channel").messages_since == 0
    assert repository.pending_coverage() == []


def test_only_one_worker_processes_each_request(repository, core):
    for index in range(6):
        seed(repository, core, [msg(f"c{index}", channel=f"ch{index}")], channel=f"ch{index}")
        received(repository, core, channel=f"ch{index}")
    provider = Spy()
    workers = [worker_for(repository, core, provider) for _ in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        counts = list(pool.map(lambda worker: worker.drain(), workers))
    assert sum(counts) == 6 and provider.calls == 6
    assert repository.digests.count_documents({}) == 6
    assert repository.summaries.count_documents({"messageState": "completada"}) == 6


# --- Límites de ventana ---

def test_limit_window_applies_age_count_and_size_from_the_newest_message(monkeypatch):
    end = BASE + timedelta(days=30)
    items = [msg(f"m{index}", "x" * 10, end - timedelta(days=9 - index)) for index in range(10)]  # 9..0 días
    kept, omitted = limit_window(items, end)
    assert [m.id for m in kept] == [f"m{index}" for index in range(2, 10)]  # 7 días
    assert [m.id for m in omitted] == ["m0", "m1"]
    monkeypatch.setattr(config, "LLM_MAX_MESSAGES", 3)
    assert [m.id for m in limit_window(items, end)[0]] == ["m7", "m8", "m9"]
    monkeypatch.setattr(config, "LLM_MAX_INPUT_CHARS", 25)
    assert [m.id for m in limit_window(items, end)[0]] == ["m8", "m9"]
    monkeypatch.setattr(config, "LLM_MAX_INPUT_CHARS", 1)
    assert [m.id for m in limit_window(items, end)[0]] == ["m9"]  # Siempre conserva el más reciente.
    assert limit_window([], end) == ([], [])


def test_omitted_messages_are_reported_in_the_digest_and_not_resummarized(repository, core, monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_MESSAGES", 2)
    seed(repository, core, [msg(f"m{index}", at=BASE + timedelta(minutes=index)) for index in range(5)])
    summary = received(repository, core)
    worker_for(repository, core).drain()
    done = repository.get_summary(summary.id)
    digest = repository.get_digest_for_summary(summary.id)
    assert (digest.message_count, digest.omitted_count, digest.first_message_id) == (2, 3, "m3")
    assert "se omitieron 3 mensajes anteriores" in done.content
    assert repository.get_checkpoint("channel").messages_since == 0  # El umbral no se vuelve a disparar.


def test_a_window_with_only_messages_older_than_the_limit_fails_explicitly(repository, core):
    seed(repository, core, [msg("old", at=utc_now() - timedelta(days=10))])
    summary = received(repository, core)
    worker_for(repository, core).drain()
    assert "antigüedad máxima de 7 días" in repository.get_summary(summary.id).error


# --- Proveedor ---

def test_mock_provider_is_deterministic_and_provider_selection_is_explicit():
    request = LLMRequest("s", "c", [{"id": "m", "author_id": "u", "author_name": "Ana",
                                      "content": "Hola", "created_at": "2026-10-18T10:00:00+00:00"}])
    assert MockProvider().generate(request) == MockProvider().generate(request)
    assert isinstance(build_provider("mock"), MockProvider)
    with pytest.raises(ValueError, match="gemini"):
        build_provider("gemini")
    with pytest.raises(ValueError):
        build_provider("otro")


# --- Despertar por aviso (hilo + RabbitMQ) ---

def _wait(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition was not met")


def test_request_flows_from_post_through_broker_to_the_worker_without_polling(api, repository, core, broker_url,
                                                                              monkeypatch):
    monkeypatch.setattr(config, "WORKER_POLL_SECONDS", 3600)
    monkeypatch.setattr(config, "OUTBOX_POLL_SECONDS", 3600)
    seed(repository, core, [msg("m1"), msg("m2", at=BASE + timedelta(minutes=1))])
    worker = worker_for(repository, core)
    worker.start()
    runtime = EventBroker(repository, url=broker_url, queue_prefix=f"test_{uuid4().hex}",
                          on_request_received=worker.notify)
    runtime.start()
    api.app.state.broker = runtime  # El POST avisa a la outbox sin esperar al sondeo.
    try:
        assert runtime.ready.wait(15)
        summary_id = api.post("/api/v1/channels/channel/summaries").json()["summary_id"]
        _wait(lambda: api.get(f"/api/v1/summaries/{summary_id}").json()["messageState"] == "completada")
        assert "2 mensajes" in api.get(f"/api/v1/summaries/{summary_id}").json()["content"]
        assert api.get("/api/v1/channels/channel/summaries/latest").json()["id"] == summary_id
    finally:
        runtime.stop()
        worker.stop()
        with pika.BlockingConnection(runtime.connection_parameters()) as connection:
            channel = connection.channel()
            for queue in (runtime.history_queue, runtime.requests_queue, runtime.errors_queue):
                channel.queue_delete(queue=queue)
            channel.exchange_delete(exchange=runtime.dead_exchange)
