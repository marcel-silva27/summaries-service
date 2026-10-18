from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models import Checkpoint, Digest, LocalMessage, MessageState, Summary, TimeInterval


NOW = datetime(2026, 10, 18, 15, tzinfo=timezone.utc)


def test_dates_are_utc_and_have_bson_precision():
    start = datetime(2026, 10, 18, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-3)))
    interval = TimeInterval(start=start, end=start + timedelta(hours=1))
    assert interval.start == NOW.replace(microsecond=123000)
    summary = Summary(channel_id="channel")
    assert summary.created_at.utcoffset() == timedelta(0)
    assert summary.created_at.microsecond % 1000 == 0


@pytest.mark.parametrize("start,end", [
    (NOW, NOW), (NOW, NOW - timedelta(seconds=1)),
    (NOW.replace(tzinfo=None), NOW + timedelta(seconds=1)),
    (NOW, (NOW + timedelta(seconds=1)).replace(tzinfo=None)),
])
def test_reject_invalid_intervals(start, end):
    with pytest.raises(ValidationError):
        TimeInterval(start=start, end=end)


@pytest.mark.parametrize("values", [
    {"messageState": MessageState.completada},
    {"messageState": MessageState.completada, "content": " "},
    {"messageState": MessageState.completada, "content": "ok", "error": "error"},
    {"messageState": MessageState.fallida},
    {"messageState": MessageState.fallida, "error": " "},
    {"messageState": MessageState.fallida, "error": "error", "content": "ok"},
    {"messageState": MessageState.pendiente, "content": "ok"},
    {"messageState": MessageState.procesando, "error": "error"},
    {"created_at": NOW, "updated_at": NOW - timedelta(seconds=1)},
])
def test_reject_inconsistent_summary(values):
    with pytest.raises(ValidationError):
        Summary(channel_id="channel", **values)


@pytest.mark.parametrize("values", [
    {"last_message_id": "message"}, {"last_message_at": NOW},
    {"messages_since": -1}, {"last_summary_id": "summary"},
])
def test_reject_incomplete_checkpoint(values):
    with pytest.raises(ValidationError):
        Checkpoint(channel_id="channel", **values)


@pytest.mark.parametrize("values", [
    {"content": ""}, {"content": "x" * 4001}, {"channel_id": " "},
    {"created_at": NOW.replace(tzinfo=None)}, {"unknown": "value"},
])
def test_reject_invalid_messages(values):
    message = dict(id="message", channel_id="channel", author_id="author", content="text", created_at=NOW)
    with pytest.raises(ValidationError):
        LocalMessage(**(message | values))


@pytest.mark.parametrize("values", [
    {"content": " "}, {"message_count": 0}, {"last_message_at": NOW + timedelta(hours=1)},
    {"last_message_at": NOW - timedelta(seconds=1)}, {"message_count": 2},
])
def test_reject_inconsistent_digest(values):
    digest = dict(summary_id="summary", channel_id="channel", content="result", message_count=1,
                  first_message_id="m1", last_message_id="m1", last_message_at=NOW,
                  timeInterval=TimeInterval(start=NOW, end=NOW + timedelta(hours=1)))
    with pytest.raises(ValidationError):
        Digest(**(digest | values))


def test_public_json_does_not_expose_bson():
    summary = Summary(channel_id="channel")
    result = summary.model_dump(mode="json")
    assert result["messageState"] == "pendiente"
    assert result["created_at"].endswith("Z")
    assert "_id" not in result
