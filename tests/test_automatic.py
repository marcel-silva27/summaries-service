"""Paso 9: resumen automático al entrar a un canal (entrada publicada por un Gateway)."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pika
import pytest
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError  # noqa: F401

from app import config
from app.errors import PersistenceConflict
from app.events import EventBroker, make_event
from app.models import ChannelEntry, MessageState, SummaryRequest, utc_now
from app.tools import publish_channel_entered
from test_worker import BASE, Core, _cleanup, _wait, msg, seed, worker_for


@pytest.fixture(autouse=True)
def fast_retries_and_default_limits(monkeypatch):
    monkeypatch.setattr(config, "WORKER_RETRY_SECONDS", 0.001)
    monkeypatch.setattr(config, "LLM_MAX_DAYS", 7)
    monkeypatch.setattr(config, "LLM_MAX_MESSAGES", 500)
    monkeypatch.setattr(config, "LLM_MAX_INPUT_CHARS", 400_000)
    monkeypatch.setattr(config, "MESSAGE_THRESHOLD", 3)


@pytest.fixture
def core():
    return Core()


def messages(count, start=0, at=BASE, channel="channel"):
    return [msg(f"m{start + index:04}", at=at + timedelta(seconds=start + index), channel=channel)
            for index in range(count)]


def enter(repository, entry_id=None, channel="channel", user="u1"):
    entry_id = entry_id or str(uuid4())
    repository.receive_channel_entry(ChannelEntry(channel_id=channel, user_id=user, entry_id=entry_id))
    return entry_id


def entry_state(repository, entry_id):
    return repository.entries.find_one({"entry_id": entry_id}, {"_id": 0})


def automatic(repository, channel="channel"):
    return list(repository.summaries.find({"channel_id": channel, "_entry_id": {"$exists": True}}))


def receive(repository, summary_id):
    delivery = repository.summaries.find_one({"id": summary_id})["_request_delivery"]
    repository.receive_summary_request(delivery["event"])


def test_threshold_is_strict_and_a_request_needs_more_messages_than_the_threshold(repository, core):
    seed(repository, core, messages(3))
    first = enter(repository)
    worker_for(repository, core).drain()
    assert (entry_state(repository, first)["status"], entry_state(repository, first)["outcome"]) == (
        "done", "below_threshold")
    assert automatic(repository) == []
    seed(repository, core, messages(1, start=3))  # 4 > 3
    second = enter(repository)
    worker_for(repository, core).drain()
    state = entry_state(repository, second)
    assert state["outcome"] == "requested" and len(automatic(repository)) == 1
    assert state["summary_id"] == automatic(repository)[0]["id"]


def test_fifty_messages_with_the_default_threshold_do_not_trigger_but_fifty_one_do(repository, core, monkeypatch):
    monkeypatch.setattr(config, "MESSAGE_THRESHOLD", 50)
    seed(repository, core, messages(50))
    worker = worker_for(repository, core)
    enter(repository)
    worker.drain()
    assert automatic(repository) == []
    seed(repository, core, messages(1, start=50))
    enter(repository)
    worker.drain()
    assert len(automatic(repository)) == 1


def test_the_automatic_request_is_a_normal_request_attributed_to_the_user(repository, core):
    seed(repository, core, messages(4))
    entry_id = enter(repository, user="u2")
    worker = worker_for(repository, core)
    worker.drain()
    document = automatic(repository)[0]
    assert (document["requested_by"], document["messageState"], document["timeInterval"]) == ("u2", "pendiente", None)
    event = document["_request_delivery"]["event"]
    assert event["type"] == "summary.requested" and event["subject_id"] == document["id"]
    assert event["data"] == {"channel_id": "channel", "requested_by": "u2", "timeInterval": None,
                             "trigger": "automatic", "entry_id": entry_id}
    receive(repository, document["id"])  # Lo que haría el consumidor tras publicarlo en el broker.
    worker.drain()
    done = repository.get_summary(document["id"])
    assert done.messageState == MessageState.completada and "4 mensajes" in done.content
    assert repository.get_checkpoint("channel").messages_since == 0
    assert "_automatic_active" not in repository.summaries.find_one({"id": document["id"]})


def test_the_same_entry_is_stored_and_evaluated_once_even_if_the_event_is_repeated(repository, core):
    seed(repository, core, messages(4))
    entry = ChannelEntry(channel_id="channel", user_id="u1", entry_id="entry-1")
    assert repository.receive_channel_entry(entry) is True
    assert repository.receive_channel_entry(entry) is False
    with pytest.raises(PersistenceConflict):
        repository.receive_channel_entry(ChannelEntry(channel_id="other", user_id="u1", entry_id="entry-1"))
    worker = worker_for(repository, core)
    worker.drain()
    assert repository.receive_channel_entry(entry) is False  # Repetido tras procesarse: no reabre nada.
    worker.drain()
    assert repository.entries.count_documents({}) == 1 and len(automatic(repository)) == 1
    assert entry_state(repository, "entry-1")["status"] == "done"


def test_reprocessing_an_entry_after_a_crash_finds_its_existing_request(repository, core):
    seed(repository, core, messages(4))
    entry_id = enter(repository)
    worker = worker_for(repository, core)
    worker.drain()
    created = automatic(repository)[0]["id"]
    repository.entries.update_one({"entry_id": entry_id}, {"$set": {"status": "pending"}})  # Cayó antes de cerrarla.
    worker.drain()
    assert [d["id"] for d in automatic(repository)] == [created]
    assert entry_state(repository, entry_id)["summary_id"] == created


def test_only_one_automatic_request_is_active_per_channel_and_it_is_released_when_it_ends(repository, core):
    seed(repository, core, messages(4))
    worker = worker_for(repository, core)
    first, second = enter(repository), enter(repository)
    worker.drain()
    assert sorted(entry_state(repository, e)["outcome"] for e in (first, second)) == ["already_active", "requested"]
    assert len(automatic(repository)) == 1
    active = automatic(repository)[0]["id"]
    receive(repository, active)
    worker.drain()  # Termina: libera el canal.
    seed(repository, core, messages(4, start=100, at=BASE + timedelta(hours=1)))
    third = enter(repository)
    worker.drain()
    assert entry_state(repository, third)["outcome"] == "requested" and len(automatic(repository)) == 2


def test_a_failed_automatic_request_also_releases_the_channel(repository, core):
    seed(repository, core, messages(4))
    worker = worker_for(repository, core)
    enter(repository)
    worker.drain()
    summary_id = automatic(repository)[0]["id"]
    repository.fail_summary(summary_id, "falló")
    assert "_automatic_active" not in repository.summaries.find_one({"id": summary_id})
    seed(repository, core, messages(1, start=50))
    other = enter(repository)
    worker.drain()
    assert entry_state(repository, other)["outcome"] == "requested"


def test_simultaneous_entries_from_several_workers_open_a_single_request(repository, core):
    seed(repository, core, messages(5))
    entries = [enter(repository) for _ in range(8)]
    workers = [worker_for(repository, core) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda worker: worker.drain(), workers))
    outcomes = sorted(entry_state(repository, e)["outcome"] for e in entries)
    assert outcomes == ["already_active"] * 7 + ["requested"]
    assert len(automatic(repository)) == 1


def test_an_entry_abandoned_in_processing_is_recovered_when_its_lease_expires(repository, core):
    seed(repository, core, messages(4))
    entry_id = enter(repository)
    assert repository.claim_entry("dead-worker", 120)["attempts"] == 1
    assert repository.claim_entry("other", 120) is None  # Vigente: nadie más la toma.
    repository.entries.update_one({"entry_id": entry_id}, {"$set": {"lease.expires_at": BASE}})
    worker_for(repository, core).drain()
    assert entry_state(repository, entry_id)["outcome"] == "requested"
    assert entry_state(repository, entry_id)["attempts"] == 2


def test_entries_with_unknown_user_or_channel_are_closed_without_a_request(repository, core):
    seed(repository, core, messages(4))
    ghost = enter(repository, user="ghost")
    worker_for(repository, core).drain()
    assert entry_state(repository, ghost)["outcome"] == "invalid_reference"
    core.channel_status = 404
    missing = enter(repository)
    worker_for(repository, core).drain()
    assert entry_state(repository, missing)["outcome"] == "invalid_reference"
    assert automatic(repository) == []


def test_an_unavailable_core_is_retried_and_then_the_entry_is_closed(repository, core):
    seed(repository, core, messages(4))
    core.channel_status = 503
    entry_id = enter(repository)
    worker_for(repository, core).drain()
    state = entry_state(repository, entry_id)
    assert (state["outcome"], state["attempts"]) == ("dependency_unavailable", config.WORKER_MAX_ATTEMPTS)
    assert automatic(repository) == []


def test_messages_the_core_never_published_are_recovered_before_evaluating(repository, core):
    core.messages["channel"] = messages(4)  # Solo existen en la API; el historial local está vacío.
    entry_id = enter(repository)
    worker_for(repository, core).drain()
    assert entry_state(repository, entry_id)["outcome"] == "requested"
    assert repository.count_messages("channel") == 4


def test_messages_older_than_the_window_do_not_count_toward_the_threshold(repository, core):
    old = utc_now() - timedelta(days=9)
    seed(repository, core, messages(10, at=old))
    stale = enter(repository)
    worker = worker_for(repository, core)
    worker.drain()
    assert entry_state(repository, stale)["outcome"] == "no_recent_messages"
    seed(repository, core, messages(2, start=500))  # 2 recientes + 10 viejos: solo cuentan los recientes.
    below = enter(repository)
    worker.drain()
    assert entry_state(repository, below)["outcome"] == "below_threshold"
    seed(repository, core, messages(2, start=600))
    enough = enter(repository)
    worker.drain()
    assert entry_state(repository, enough)["outcome"] == "requested"
    assert repository.get_checkpoint("channel").messages_since >= 4


def test_manual_requests_do_not_take_part_in_the_automatic_exclusion(repository, core):
    seed(repository, core, messages(4))
    repository.create_summary("channel", SummaryRequest())
    repository.create_summary("channel", SummaryRequest())
    enter(repository)
    worker_for(repository, core).drain()
    assert len(automatic(repository)) == 1


def test_channel_entry_contract_is_strict():
    ChannelEntry(channel_id="c", user_id="u", entry_id="e")
    for data in ({"channel_id": "c", "user_id": "u"}, {"channel_id": "c", "user_id": "u", "entry_id": " "},
                 {"channel_id": "c", "user_id": "u", "entry_id": "e", "extra": 1}):
        with pytest.raises(ValidationError):
            ChannelEntry(**data)


# --- Extremo a extremo con el broker ---

def test_a_gateway_event_flows_through_the_broker_to_a_generated_summary(repository, core, broker_url, monkeypatch):
    monkeypatch.setattr(config, "WORKER_POLL_SECONDS", 3600)
    monkeypatch.setattr(config, "OUTBOX_POLL_SECONDS", 3600)
    monkeypatch.setattr(config, "BROKER_URL", broker_url)
    seed(repository, core, messages(5))
    worker = worker_for(repository, core)
    worker.start()
    runtime = EventBroker(repository, url=broker_url, queue_prefix=f"test_{uuid4().hex}",
                          on_request_received=worker.notify)
    worker.on_result = runtime.notify
    runtime.start()
    connection = None
    try:
        assert runtime.ready.wait(15)
        connection = pika.BlockingConnection(runtime.connection_parameters())
        channel = connection.channel()
        results = f"test_results_{uuid4().hex}"
        channel.queue_declare(queue=results, durable=True)
        channel.queue_bind(queue=results, exchange="summaries", routing_key="summary.generated")

        assert publish_channel_entered.main(["channel", "u1", "entry-e2e"]) == 0
        assert publish_channel_entered.main(["channel", "u1", "entry-e2e"]) == 0  # Reintento del Gateway.

        def result():
            method, _, body = channel.basic_get(results, auto_ack=True)
            return json.loads(body) if method else None

        event = _wait(result)
        summary = automatic(repository)[0]
        assert event["type"] == "summary.generated" and event["subject_id"] == summary["id"]
        assert event["data"]["requested_by"] == "u1" and event["data"]["messageState"] == "completada"
        assert repository.get_summary(summary["id"]).messageState == MessageState.completada
        _wait(lambda: entry_state(repository, "entry-e2e")["status"] == "done")
        assert len(automatic(repository)) == 1 and repository.entries.count_documents({}) == 1
        assert channel.basic_get(results)[0] is None
    finally:
        runtime.stop()
        worker.stop()
        if connection is not None:
            connection.close()
        _cleanup(runtime)


def test_invalid_gateway_events_are_dead_lettered_instead_of_stored(repository, broker_url, broker):
    good = make_event("chat.channel.entered", "channel", {"channel_id": "channel", "user_id": "u", "entry_id": "e"})
    bad = [good | {"subject_id": "other-channel"}, good | {"data": {"channel_id": "channel", "user_id": "u"}},
           good | {"type": "chat.channel.left"}]
    with pika.BlockingConnection(broker.connection_parameters()) as connection:
        channel = connection.channel()
        for event in bad:
            channel.basic_publish(exchange="gateway", routing_key="chat.channel.entered",
                                  body=json.dumps(event).encode())
        _wait(lambda: channel.queue_declare(queue=broker.errors_queue, passive=True).method.message_count == 3)
    assert repository.entries.count_documents({}) == 0
