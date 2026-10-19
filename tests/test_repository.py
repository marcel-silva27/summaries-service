from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import time
from threading import Barrier

import pytest
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from app.errors import PersistenceConflict, SummaryNotFound, SummaryStateConflict
from app.models import Checkpoint, Digest, LocalMessage, MessageState, Summary, SummaryRequest, TimeInterval
from app.repository import MongoRepository


NOW = datetime(2026, 10, 18, 15, tzinfo=timezone.utc)
WINDOW = TimeInterval(start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1))


def message(id="m1", channel="channel", at=NOW):
    return LocalMessage(id=id, channel_id=channel, author_id="author", content=f"Message {id}", created_at=at)


def digest(summary, last_id="m1", last_at=NOW, **values):
    data = dict(summary_id=summary.id, channel_id=summary.channel_id, content="Decisión: entregar el viernes.",
                timeInterval=summary.timeInterval or WINDOW, message_count=1,
                first_message_id=last_id, last_message_id=last_id, last_message_at=last_at)
    return Digest(**(data | values))


def completed(repository, last_id="m1", last_at=NOW, request=None):
    summary = repository.create_summary("channel", request)
    repository.start_summary(summary.id)
    repository.save_digest(digest(summary, last_id, last_at))
    return repository.complete_summary(summary.id)


def concurrent(work, count=8):
    barrier = Barrier(count)

    def run(index):
        barrier.wait(timeout=10)
        return work(index)

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(run, range(count)))


def test_summary_roundtrip_and_persistence_with_new_client(repository, mongo_client):
    request = SummaryRequest(user_id="user", timeInterval=WINDOW)
    summary = repository.create_summary("channel", request)
    assert summary.messageState == MessageState.pendiente
    assert repository.get_summary(summary.id) == summary
    raw = repository.summaries.find_one({"id": summary.id})
    assert isinstance(raw["created_at"], datetime)
    assert isinstance(raw["timeInterval"]["start"], datetime)
    with MongoClient(mongo_client.address[0], mongo_client.address[1], tz_aware=True) as other_client:
        other = MongoRepository(other_client[repository.database.name])
        assert other.get_summary(summary.id) == summary
        assert "_id" not in other.get_summary(summary.id).model_dump(mode="json")


def test_indexes_are_idempotent_and_enforce_unique_ids(repository):
    repository.ensure_indexes()
    repository.ensure_indexes()
    summary = repository.create_summary("channel")
    with pytest.raises(DuplicateKeyError):
        repository.summaries.insert_one(summary.model_dump())
    assert set(repository.database.list_collection_names()) == {"summaries", "digests", "checkpoints", "messages"}


def test_missing_summary_raises_explicit_404(repository):
    for operation in (repository.get_summary, repository.start_summary, repository.complete_summary):
        with pytest.raises(SummaryNotFound):
            operation("missing")
    with pytest.raises(SummaryNotFound):
        repository.latest_summary("missing-channel")


def test_state_machine_and_terminal_idempotency(repository):
    summary = repository.create_summary("channel")
    with pytest.raises(PersistenceConflict):
        repository.complete_summary(summary.id)
    with pytest.raises(SummaryStateConflict):
        repository.save_digest(digest(summary))
    assert repository.start_summary(summary.id).messageState == MessageState.procesando
    result = repository.save_digest(digest(summary))
    done = repository.complete_summary(summary.id)
    assert done.content == result.content
    assert done.error is None
    assert done.updated_at >= done.created_at
    assert repository.complete_summary(summary.id) == done
    with pytest.raises(SummaryStateConflict):
        repository.start_summary(summary.id)
    with pytest.raises(SummaryStateConflict):
        repository.fail_summary(summary.id, "late error")


@pytest.mark.parametrize("start", [True, False])
def test_failure_is_terminal_from_pending_or_processing(repository, start):
    summary = repository.create_summary("channel")
    if start:
        repository.start_summary(summary.id)
    failed = repository.fail_summary(summary.id, "LLM unavailable")
    assert failed.messageState == MessageState.fallida
    assert failed.content is None
    assert repository.fail_summary(summary.id, "LLM unavailable") == failed
    with pytest.raises(SummaryStateConflict):
        repository.fail_summary(summary.id, "different error")
    with pytest.raises(SummaryStateConflict):
        repository.start_summary(summary.id)
    with pytest.raises(ValueError):
        repository.fail_summary(summary.id, " ")


def test_only_one_worker_claims_summary(repository):
    summary = repository.create_summary("channel")

    def claim(_):
        try:
            repository.start_summary(summary.id)
            return True
        except SummaryStateConflict:
            return False

    assert concurrent(claim).count(True) == 1


def test_concurrent_success_and_failure_cannot_overwrite_each_other(repository):
    summary = repository.create_summary("channel")
    repository.start_summary(summary.id)
    repository.save_digest(digest(summary))

    def finish(index):
        try:
            return (repository.complete_summary(summary.id) if index % 2 else
                    repository.fail_summary(summary.id, "timeout")).messageState
        except SummaryStateConflict:
            return None

    results = concurrent(finish)
    state = repository.get_summary(summary.id).messageState
    assert state in (MessageState.completada, MessageState.fallida)
    assert {result for result in results if result is not None} == {state}


def test_latest_ignores_unfinished_and_failed_and_orders_ties(repository):
    done1, done2 = completed(repository), completed(repository)
    pending = repository.create_summary("channel")
    failed = repository.create_summary("channel")
    repository.fail_summary(failed.id, "timeout")
    for index, item in enumerate((done1, done2, pending, failed)):
        repository.summaries.update_one({"id": item.id}, {"$set": {"created_at": NOW, "updated_at": NOW}})
    expected = sorted((done1.id, done2.id), reverse=True)[0]
    assert repository.latest_summary("channel").id == expected
    all_ids = sorted([done1.id, done2.id, pending.id, failed.id], reverse=True)
    assert [s.id for s in repository.list_summaries("channel")] == all_ids
    assert [s.id for s in repository.list_summaries("channel", limit=2, offset=1)] == all_ids[1:3]
    assert repository.list_summaries("other") == []


def test_digest_is_immutable_idempotent_and_linked_to_request(repository):
    summary = repository.create_summary("channel", SummaryRequest(timeInterval=WINDOW))
    repository.start_summary(summary.id)
    original = repository.save_digest(digest(summary))
    assert repository.save_digest(digest(summary)) == original
    assert repository.get_digest(original.id) == original
    assert repository.get_digest("missing") is None
    assert repository.get_digest_for_summary("missing") is None
    with pytest.raises(PersistenceConflict):
        repository.save_digest(digest(summary, content="different result"))
    with pytest.raises(PersistenceConflict):
        repository.save_digest(digest(summary, channel_id="other"))
    with pytest.raises(PersistenceConflict):
        repository.save_digest(digest(summary, timeInterval=TimeInterval(start=NOW, end=WINDOW.end)))


def test_concurrent_identical_digests_have_one_persisted_result(repository):
    summary = repository.create_summary("channel")
    repository.start_summary(summary.id)
    results = concurrent(lambda _: repository.save_digest(digest(summary)))
    assert len({item.id for item in results}) == 1
    assert repository.digests.count_documents({}) == 1


def test_message_replays_are_idempotent_and_conflicts_do_not_overwrite(repository):
    original = message()
    assert concurrent(lambda _: repository.upsert_message(original)).count(True) == 1
    assert repository.count_messages("channel") == 1
    assert repository.get_checkpoint("channel").messages_since == 1
    assert repository.get_message("m1") == original
    assert repository.get_message("missing") is None
    with pytest.raises(PersistenceConflict):
        repository.upsert_message(message(channel="other"))
    with pytest.raises(PersistenceConflict):
        repository.upsert_message(LocalMessage(**(original.model_dump() | {"content": "changed"})))
    assert repository.get_message("m1") == original


def test_message_window_is_sorted_half_open_channel_scoped_and_pageable(repository):
    for item in (message("m3", at=NOW + timedelta(seconds=1)), message("m2"), message("m1"),
                 message("excluded", at=NOW + timedelta(seconds=2)), message("other", channel="other")):
        repository.upsert_message(item)
    window = TimeInterval(start=NOW, end=NOW + timedelta(seconds=2))
    assert [m.id for m in repository.list_messages("channel", window)] == ["m1", "m2", "m3"]
    assert repository.count_messages("channel", window) == 3
    first_page = repository.list_messages("channel", window, limit=1)
    cursor = Checkpoint(channel_id="channel", last_message_id=first_page[-1].id,
                        last_message_at=first_page[-1].created_at)
    assert [m.id for m in repository.list_messages("channel", window, after=cursor)] == ["m2", "m3"]
    with pytest.raises(ValueError):
        repository.count_messages("other", after=cursor)


def test_checkpoint_retains_messages_received_while_generation_runs(repository):
    repository.upsert_message(message("m1"))
    summary = repository.create_summary("channel")
    repository.start_summary(summary.id)
    repository.save_digest(digest(summary))
    repository.upsert_message(message("m2"))  # Igual timestamp; desempate por ID.
    repository.upsert_message(message("m3", at=NOW + timedelta(seconds=1)))
    with pytest.raises(SummaryStateConflict):
        repository.advance_checkpoint(summary.id)
    repository.complete_summary(summary.id)
    checkpoint = repository.advance_checkpoint(summary.id)
    assert checkpoint.messages_since == 2
    assert checkpoint.last_message_id == "m1"
    assert checkpoint.last_summary_id == summary.id
    assert repository.advance_checkpoint(summary.id) == checkpoint
    repository.upsert_message(message("m3", at=NOW + timedelta(seconds=1)))
    assert repository.get_checkpoint("channel").messages_since == 2
    assert "messages_since" not in repository.checkpoints.find_one({"channel_id": "channel"})


def test_checkpoint_is_monotonic_under_concurrent_completions(repository):
    summaries = [completed(repository, f"m{i}", NOW + timedelta(seconds=i)) for i in range(8)]
    concurrent(lambda index: repository.advance_checkpoint(summaries[index].id))
    checkpoint = repository.get_checkpoint("channel")
    assert checkpoint.last_message_id == "m7"
    assert checkpoint.last_summary_id == summaries[-1].id
    assert repository.checkpoints.count_documents({}) == 1
    repository.advance_checkpoint(summaries[0].id)
    assert repository.get_checkpoint("channel") == checkpoint


def test_on_demand_summary_does_not_advance_incremental_checkpoint(repository):
    summary = completed(repository, request=SummaryRequest(timeInterval=WINDOW))
    with pytest.raises(PersistenceConflict):
        repository.advance_checkpoint(summary.id)
    assert repository.get_checkpoint("channel").last_message_id is None
    assert repository.checkpoints.count_documents({}) == 0


def test_late_historical_messages_are_retained_without_rewinding_cursor(repository):
    summary = completed(repository)
    checkpoint = repository.advance_checkpoint(summary.id)
    repository.upsert_message(message("old", at=NOW - timedelta(seconds=10)))
    current = repository.get_checkpoint("channel")
    assert current.last_message_id == checkpoint.last_message_id
    assert current.last_summary_id == checkpoint.last_summary_id
    assert current.messages_since == 1
    assert [m.id for m in repository.list_messages("channel", WINDOW)] == ["old"]


@pytest.mark.parametrize("limit,offset", [(0, 0), (-1, 0), (1, -1)])
def test_invalid_pagination_is_not_an_unbounded_query(repository, limit, offset):
    with pytest.raises(ValueError):
        repository.list_summaries("channel", limit=limit, offset=offset)


def _window_service(repository, source, *, unavailable=False):
    import httpx
    from app.clients.core_client import CoreClient
    from app.services import SummaryService

    def handler(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(503) if unavailable else httpx.Response(200, json=source)
        return httpx.Response(200, json={"id": "channel", "name": "General"})

    core = CoreClient(channels_url="http://channels", messages_url="http://messages",
                      transport=httpx.MockTransport(handler))
    return SummaryService(repository, core)


def test_window_reconstructs_more_than_one_page_of_missing_events(repository):
    source = [message(f"m{i:04}", at=NOW).model_dump(mode="json") for i in reversed(range(1005))]
    summary = repository.create_summary("channel", SummaryRequest(timeInterval=WINDOW))
    service = _window_service(repository, source)
    try:
        window = service.build_window(summary.id, page_size=1000)
        assert len(window.messages) == 1005
        assert [item.id for item in window.messages] == sorted(item["id"] for item in source)
        assert window.time_interval == WINDOW
        assert repository.count_messages("channel") == 1005
        assert service.build_window(summary.id, page_size=127).messages == window.messages
    finally:
        service.core.close()


def test_window_obeys_half_open_interval_and_keeps_other_history(repository):
    interval = TimeInterval(start=NOW, end=NOW + timedelta(seconds=2))
    source = [message("before", at=NOW - timedelta(seconds=1)), message("first"),
              message("inside", at=NOW + timedelta(seconds=1)),
              message("end", at=interval.end)]
    repository.upsert_message(message("other", channel="other"))
    summary = repository.create_summary("channel", SummaryRequest(timeInterval=interval))
    service = _window_service(repository, [item.model_dump(mode="json") for item in source])
    try:
        assert [item.id for item in service.build_window(summary.id, page_size=1).messages] == ["first", "inside"]
        assert repository.count_messages("channel") == 4
        assert repository.count_messages("other") == 1
    finally:
        service.core.close()


def test_incremental_window_uses_checkpoint_and_freezes_cutoff_at_request_time(repository):
    baseline = completed(repository, "m1", NOW)
    repository.advance_checkpoint(baseline.id)
    summary = repository.create_summary("channel")
    end = NOW + timedelta(seconds=2)
    repository.summaries.update_one({"id": summary.id}, {"$set": {"created_at": end, "updated_at": end}})
    source = [message("old", at=NOW - timedelta(seconds=1)), message("m1"), message("m2"),
              message("m3", at=NOW + timedelta(seconds=1)), message("future", at=end)]
    service = _window_service(repository, [item.model_dump(mode="json") for item in source])
    try:
        window = service.build_window(summary.id, page_size=1)
        assert [item.id for item in window.messages] == ["old", "m2", "m3"]
        assert window.time_interval == TimeInterval(start=NOW - timedelta(seconds=1), end=end)
        assert repository.get_checkpoint("channel").last_summary_id == baseline.id
        assert repository.get_message("future") is not None
        assert repository.get_message("old") is not None
    finally:
        service.core.close()


def test_empty_window_does_not_generate_a_digest_or_change_request_state(repository):
    summary = repository.create_summary("channel")
    service = _window_service(repository, [])
    try:
        window = service.build_window(summary.id)
        assert window.messages == ()
        assert window.time_interval is None
        assert repository.get_summary(summary.id).messageState == MessageState.pendiente
        assert repository.digests.count_documents({}) == 0
        with pytest.raises(ValueError):
            service.build_window(summary.id, page_size=0)
    finally:
        service.core.close()


def test_window_does_not_hide_failed_reconciliation_with_partial_local_history(repository):
    from app.errors import DependencyUnavailable

    repository.upsert_message(message())
    summary = repository.create_summary("channel")
    service = _window_service(repository, [], unavailable=True)
    try:
        with pytest.raises(DependencyUnavailable):
            service.build_window(summary.id)
    finally:
        service.core.close()


def _wait_for(predicate, timeout=10):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError("Expected asynchronous effect did not occur")


def _publish_to_broker(broker, key, payload, *, exchange="chat"):
    import json
    import pika

    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    with pika.BlockingConnection(broker.connection_parameters()) as connection:
        channel = connection.channel()
        channel.confirm_delivery()
        channel.basic_publish(exchange=exchange, routing_key=key, body=body, mandatory=True,
                              properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"))


def test_rabbit_history_consumption_is_continuous_idempotent_and_uses_own_queue(repository, broker):
    import pika
    from app.events import make_event

    original = message()
    event = make_event("chat.message.created", original.id, original.model_dump(mode="json"))
    with pika.BlockingConnection(broker.connection_parameters()) as connection:
        channel = connection.channel()
        other_queue = channel.queue_declare(queue="", exclusive=True).method.queue
        channel.queue_bind(queue=other_queue, exchange="chat", routing_key="chat.message.created")
        for _ in range(3):
            _publish_to_broker(broker, "chat.message.created", event)
        _wait_for(lambda: repository.get_message(original.id))
        assert repository.count_messages("channel") == 1
        assert repository.get_checkpoint("channel").messages_since == 1
        assert channel.basic_get(other_queue, auto_ack=True)[0] is not None


def test_rest_request_is_published_confirmed_and_received_durably(api, repository, broker):
    result = api.post("/api/v1/channels/channel/summaries", json={"user_id": "user"})
    assert result.status_code == 202
    summary_id = result.json()["summary_id"]
    _wait_for(lambda: repository.received_requests())
    _wait_for(lambda: not repository.pending_request_events())
    assert repository.received_requests()[0].id == summary_id
    assert api.get(f"/api/v1/summaries/{summary_id}").json()["messageState"] == "pendiente"
    delivery = repository.summaries.find_one({"id": summary_id})["_request_delivery"]
    _publish_to_broker(broker, "summary.requested", delivery["event"], exchange="summaries")
    assert repository.get_summary(summary_id).messageState == MessageState.pendiente


@pytest.mark.parametrize("payload", [b"not json", b"{}", b"[]",
    {"type": "chat.message.created", "subject_id": "wrong", "occurred_at": NOW.isoformat(),
     "data": message().model_dump(mode="json")},
    {"type": "summary.requested", "subject_id": "m1", "occurred_at": NOW.isoformat(), "data": {}},
])
def test_invalid_events_are_dead_lettered_instead_of_requeued_forever(repository, broker, payload):
    import pika

    _publish_to_broker(broker, "chat.message.created", payload)
    with pika.BlockingConnection(broker.connection_parameters()) as connection:
        channel = connection.channel()
        rejected = _wait_for(lambda: channel.basic_get(broker.errors_queue, auto_ack=True)[0])
        assert rejected is not None
    assert repository.count_messages("channel") == 0


def test_mongo_failure_nacks_event_and_conflicting_payload_is_rejected(repository, monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from pymongo.errors import ConnectionFailure
    from app import config
    from app.events import EventBroker, make_event

    monkeypatch.setattr(config, "BROKER_RETRY_SECONDS", 0.001)
    runtime = EventBroker(repository)
    channel = MagicMock()
    method = SimpleNamespace(delivery_tag=1, routing_key="chat.message.created")
    body = json.dumps(make_event("chat.message.created", "m1", message().model_dump(mode="json"))).encode()
    with monkeypatch.context() as patch:
        patch.setattr(repository, "upsert_message", MagicMock(side_effect=ConnectionFailure("storage down")))
        runtime._consume_history(channel, method, None, body)
    channel.basic_nack.assert_called_once_with(delivery_tag=1, requeue=True)
    channel.basic_ack.assert_not_called()
    repository.upsert_message(message())
    wrong = message().model_dump(mode="json") | {"content": "different"}
    body = json.dumps(make_event("chat.message.created", "m1", wrong)).encode()
    runtime._consume_history(channel, method, None, body)
    channel.basic_reject.assert_called_once_with(delivery_tag=1, requeue=False)


def test_outbox_survives_failed_publication_and_next_process_can_deliver(api, repository, broker_url):
    from unittest.mock import MagicMock
    import pika
    from app.events import EventBroker

    response = api.post("/api/v1/channels/channel/summaries")
    runtime = EventBroker(repository, url=broker_url, queue_prefix=f"test_recovery_{response.json()['summary_id']}")
    channel = MagicMock()
    channel.basic_publish.side_effect = pika.exceptions.AMQPConnectionError("broker down")
    with pytest.raises(pika.exceptions.AMQPConnectionError):
        runtime._flush_outbox(channel)
    assert len(repository.pending_request_events()) == 1
    runtime.start()
    try:
        assert runtime.ready.wait(10)
        _wait_for(lambda: repository.received_requests())
        _wait_for(lambda: not repository.pending_request_events())
    finally:
        runtime.stop()
        with pika.BlockingConnection(runtime.connection_parameters()) as connection:
            channel = connection.channel()
            for queue in (runtime.history_queue, runtime.requests_queue, runtime.errors_queue):
                channel.queue_delete(queue)
            channel.exchange_delete(runtime.dead_exchange)


def test_rabbit_reconnects_after_initial_connection_failure(repository, broker_url, monkeypatch):
    import pika
    from app import config
    from app.events import EventBroker
    from uuid import uuid4

    connect = pika.BlockingConnection
    attempts = []

    def connection(parameters):
        attempts.append(True)
        if len(attempts) == 1:
            raise pika.exceptions.AMQPConnectionError("temporary failure")
        return connect(parameters)

    monkeypatch.setattr(config, "BROKER_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(pika, "BlockingConnection", connection)
    runtime = EventBroker(repository, url=broker_url, queue_prefix=f"test_reconnect_{uuid4().hex}")
    runtime.start()
    try:
        assert runtime.ready.wait(10)
        assert len(attempts) >= 2
    finally:
        runtime.stop()
        with connect(runtime.connection_parameters()) as conn:
            channel = conn.channel()
            for queue in (runtime.history_queue, runtime.requests_queue, runtime.errors_queue):
                channel.queue_delete(queue)
            channel.exchange_delete(runtime.dead_exchange)


def _complete_window(repository, summary, window):
    repository.start_summary(summary.id)
    repository.save_digest(Digest(
        summary_id=summary.id, channel_id=summary.channel_id, timeInterval=window.time_interval,
        content="Resumen confirmado", message_count=len(window.messages),
        first_message_id=window.messages[0].id, last_message_id=window.messages[-1].id,
        last_message_at=window.messages[-1].created_at))
    repository.complete_summary(summary.id)
    return repository.advance_checkpoint(summary.id)


def test_late_messages_go_to_next_incremental_without_rewriting_old_summaries(repository):
    source = [message("a"), message("b", at=NOW + timedelta(seconds=1))]
    service = _window_service(repository, [item.model_dump(mode="json") for item in source])
    first = repository.create_summary("channel")
    cutoff = NOW + timedelta(hours=2)
    repository.summaries.update_one({"id": first.id}, {"$set": {"created_at": cutoff, "updated_at": cutoff}})
    try:
        first_window = service.build_window(first.id)
        baseline = _complete_window(repository, first, first_window)
        assert baseline.messages_since == 0
        old_summary = repository.get_summary(first.id)
        late = message("late", at=NOW - timedelta(seconds=10))
        repository.upsert_message(late)
        assert repository.get_checkpoint("channel").messages_since == 1
        second = repository.create_summary("channel")
        repository.summaries.update_one({"id": second.id}, {"$set": {"created_at": cutoff, "updated_at": cutoff}})
        second_window = service.build_window(second.id)
        assert [item.id for item in second_window.messages] == ["late"]
        # Otro tardío llega mientras se procesa la ventana ya fijada.
        later = message("also-late", at=NOW - timedelta(seconds=5))
        repository.upsert_message(later)
        assert service.build_window(second.id).messages == second_window.messages
        advanced = _complete_window(repository, second, second_window)
        assert advanced.last_message_id == baseline.last_message_id
        assert advanced.messages_since == 1
        assert repository.get_summary(first.id) == old_summary
        third = repository.create_summary("channel")
        repository.summaries.update_one({"id": third.id}, {"$set": {"created_at": cutoff, "updated_at": cutoff}})
        third_window = service.build_window(third.id)
        assert [item.id for item in third_window.messages] == ["also-late"]
        assert _complete_window(repository, third, third_window).messages_since == 0
    finally:
        service.core.close()


def test_digest_cannot_claim_coverage_of_a_different_snapshot(repository):
    summary = repository.create_summary("channel", SummaryRequest(timeInterval=WINDOW))
    service = _window_service(repository, [message("a").model_dump(mode="json"), message("b").model_dump(mode="json")])
    try:
        service.build_window(summary.id)
        repository.start_summary(summary.id)
        with pytest.raises(PersistenceConflict):
            repository.save_digest(digest(summary, "a"))
        assert repository.digests.count_documents({}) == 0
    finally:
        service.core.close()


@pytest.mark.parametrize("tamper", ["unknown-summary", "different-channel"])
def test_requested_event_cannot_create_or_change_a_request(api, repository, broker, tamper):
    import pika

    summary_id = api.post("/api/v1/channels/channel/summaries").json()["summary_id"]
    _wait_for(lambda: repository.received_requests())
    event = repository.summaries.find_one({"id": summary_id})["_request_delivery"]["event"]
    if tamper == "unknown-summary":
        event["subject_id"] = "unknown"
    else:
        event["data"]["channel_id"] = "different"
    _publish_to_broker(broker, "summary.requested", event, exchange="summaries")
    with pika.BlockingConnection(broker.connection_parameters()) as connection:
        channel = connection.channel()
        _wait_for(lambda: channel.basic_get(broker.errors_queue, auto_ack=True)[0])
    assert repository.summaries.count_documents({}) == 1
    assert repository.get_summary(summary_id).channel_id == "channel"
    assert repository.get_summary(summary_id).messageState == MessageState.pendiente


# --- Upsert en lote y publicación de la outbox por aviso ---

def test_bulk_upsert_inserts_once_is_idempotent_and_spans_batches(repository):
    items = [message(f"m{index:04d}", at=NOW + timedelta(seconds=index)) for index in range(25)]
    assert repository.upsert_messages(items, batch_size=10) == 25
    assert repository.upsert_messages(items, batch_size=10) == 0
    assert repository.upsert_messages(items + [message("new", at=NOW + timedelta(minutes=1))]) == 1
    assert repository.count_messages("channel") == 26
    assert repository.get_checkpoint("channel").messages_since == 26
    assert repository.upsert_messages([]) == 0
    with pytest.raises(ValueError):
        repository.upsert_messages(items, batch_size=0)


def test_bulk_upsert_conflicts_never_overwrite(repository):
    original = message("m1")
    repository.upsert_messages([original, message("m2")])
    changed = LocalMessage(**(original.model_dump() | {"content": "changed"}))
    with pytest.raises(PersistenceConflict):
        repository.upsert_messages([message("m3"), changed])
    with pytest.raises(PersistenceConflict):
        repository.upsert_messages([original, changed])  # Mismo ID repetido en el lote.
    with pytest.raises(PersistenceConflict):
        repository.upsert_messages([message("m1", channel="other")])
    assert repository.get_message("m1") == original


def test_bulk_upsert_marks_checkpoint_message_as_covered_and_survives_races(repository):
    summary = completed(repository, "m1")
    repository.advance_checkpoint(summary.id)
    items = [message("m1"), message("m2", at=NOW + timedelta(seconds=1))]
    repository.upsert_messages(items)
    assert repository.get_checkpoint("channel").messages_since == 1  # Solo m2 queda sin cobertura.
    shared = [message(f"r{index}", at=NOW + timedelta(seconds=index)) for index in range(50)]
    inserted = concurrent(lambda _: repository.upsert_messages(shared))
    assert sum(inserted) == 50
    assert repository.count_messages("channel") == 52


def test_build_window_reconciles_with_one_bulk_write_not_one_per_message(repository):
    from unittest.mock import MagicMock
    from app.services import SummaryService

    data = [message(f"m{index}", at=NOW + timedelta(seconds=index)).model_dump(mode="json") for index in range(30)]
    core = MagicMock()
    core.get_channel.return_value = {}
    core.list_messages.return_value = data
    summary = repository.create_summary("channel")
    spy = MagicMock(wraps=repository.messages.bulk_write)
    repository.messages.bulk_write = spy
    window = SummaryService(repository, core).build_window(summary.id)
    assert len(window.messages) == 30
    assert spy.call_count == 1


def test_request_notifies_broker_only_when_a_request_is_created(api, repository):
    from unittest.mock import MagicMock
    from app.services import SummaryService

    notify = MagicMock()
    core = api.app.state.core_client
    service = SummaryService(repository, core, notify)
    service.request_summary("channel")
    notify.assert_called_once_with()
    notify.reset_mock()
    with pytest.raises(Exception):
        service.request_summary("missing")
    notify.assert_not_called()


def test_outbox_is_published_on_notify_without_frequent_polling(repository, broker_url, monkeypatch):
    from unittest.mock import MagicMock
    from uuid import uuid4
    import pika
    from app import config
    from app.events import EventBroker, make_event
    from app.models import utc_now

    monkeypatch.setattr(config, "OUTBOX_POLL_SECONDS", 3600)
    runtime = EventBroker(repository, url=broker_url, queue_prefix=f"test_{uuid4().hex}")
    spy = MagicMock(wraps=repository.pending_request_events)
    repository.pending_request_events = spy
    runtime.start()
    try:
        assert runtime.ready.wait(15)
        _wait_for(lambda: spy.call_count >= 1)  # Publicación inicial al conectar.
        summary = repository.create_summary(
            "channel", event_factory=lambda item: make_event("summary.requested", item.id, {"channel_id": "channel"}))
        calls = spy.call_count
        time.sleep(1.5)
        assert spy.call_count == calls  # Sin aviso ni sondeo: Mongo no se consulta.
        assert repository.pending_request_events() != []
        calls = spy.call_count
        runtime.notify()
        _wait_for(lambda: repository.summaries.find_one({"id": summary.id})["_request_delivery"]["published"])
        assert spy.call_count == calls + 1
    finally:
        runtime.stop()
        with pika.BlockingConnection(runtime.connection_parameters()) as connection:
            channel = connection.channel()
            for queue in (runtime.history_queue, runtime.requests_queue, runtime.errors_queue):
                channel.queue_delete(queue=queue)
            channel.exchange_delete(exchange=runtime.dead_exchange)


def test_outbox_polling_fallback_recovers_a_missed_notification(repository, broker_url, monkeypatch):
    from uuid import uuid4
    import pika
    from app import config
    from app.events import EventBroker, make_event

    monkeypatch.setattr(config, "OUTBOX_POLL_SECONDS", 0.3)
    runtime = EventBroker(repository, url=broker_url, queue_prefix=f"test_{uuid4().hex}")
    runtime.start()
    try:
        assert runtime.ready.wait(15)
        summary = repository.create_summary(
            "channel", event_factory=lambda item: make_event("summary.requested", item.id, {"channel_id": "channel"}))
        _wait_for(lambda: repository.summaries.find_one({"id": summary.id})["_request_delivery"]["published"])
    finally:
        runtime.stop()
        with pika.BlockingConnection(runtime.connection_parameters()) as connection:
            channel = connection.channel()
            for queue in (runtime.history_queue, runtime.requests_queue, runtime.errors_queue):
                channel.queue_delete(queue=queue)
            channel.exchange_delete(exchange=runtime.dead_exchange)
