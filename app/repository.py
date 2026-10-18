"""Persistencia propia con PyMongo síncrono; las operaciones son atómicas por documento.

Invocar desde endpoints def o desde un hilo si el llamador es asíncrono.
La orquestación y la publicación de eventos pertenecen a las siguientes etapas.
"""
from typing import Annotated, Callable, TypeVar

from bson.codec_options import CodecOptions
from fastapi import Depends, Request
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from .errors import PersistenceConflict, SummaryNotFound, SummaryStateConflict
from .models import (
    Checkpoint, Digest, DomainModel, LocalMessage, MessageState, Summary,
    SummaryRequest, TimeInterval, utc_now,
)

Model = TypeVar("Model", bound=DomainModel)
NEWEST = [("created_at", DESCENDING), ("id", DESCENDING)]
CHRONOLOGICAL = [("created_at", ASCENDING), ("id", ASCENDING)]


def _decode(model: type[Model], document: dict | None) -> Model | None:
    if document is None:
        return None
    return model.model_validate({key: value for key, value in document.items()
                                if key not in ("_id", "_request_delivery", "_history", "_window")})


def _document(model: DomainModel) -> dict:
    # mode=python conserva datetime como BSON Date, no como strings JSON.
    return model.model_dump(mode="python")


def _after_cursor(checkpoint: Checkpoint) -> dict:
    if checkpoint.last_message_at is None:
        return {}
    return {"$or": [
        {"created_at": {"$gt": checkpoint.last_message_at}},
        {"created_at": checkpoint.last_message_at, "id": {"$gt": checkpoint.last_message_id}},
    ]}


class MongoRepository:
    def __init__(self, database: Database):
        self.database = database.with_options(codec_options=CodecOptions(tz_aware=True))
        self.summaries = self.database["summaries"]
        self.digests = self.database["digests"]
        self.checkpoints = self.database["checkpoints"]
        self.messages = self.database["messages"]

    def ensure_indexes(self) -> None:
        """Idempotente. Un conflicto con datos existentes debe fallar al iniciar."""
        self.summaries.create_index("id", unique=True, name="summary_id_unique")
        self.summaries.create_index(
            [("channel_id", ASCENDING), *NEWEST], name="summaries_by_channel")
        self.summaries.create_index(
            [("channel_id", ASCENDING), ("messageState", ASCENDING), *NEWEST],
            name="latest_completed_summary")
        self.summaries.create_index(
            [("messageState", ASCENDING), ("updated_at", ASCENDING)], name="pending_work")
        self.summaries.create_index(
            [("_request_delivery.published", ASCENDING), ("created_at", ASCENDING)],
            name="request_delivery_pending")
        self.summaries.create_index(
            [("_request_delivery.received", ASCENDING), ("messageState", ASCENDING)],
            name="request_inbox")
        self.digests.create_index("id", unique=True, name="digest_id_unique")
        self.digests.create_index("summary_id", unique=True, name="digest_per_summary")
        self.digests.create_index(
            [("channel_id", ASCENDING), *NEWEST], name="digests_by_channel")
        self.checkpoints.create_index("channel_id", unique=True, name="checkpoint_per_channel")
        self.messages.create_index("id", unique=True, name="message_id_unique")
        self.messages.create_index(
            [("channel_id", ASCENDING), *CHRONOLOGICAL], name="message_window")
        self.messages.create_index(
            [("channel_id", ASCENDING), ("_history.covered", ASCENDING), *CHRONOLOGICAL],
            name="uncovered_messages")

    def create_summary(self, channel_id: str, request: SummaryRequest | None = None, *,
                       event_factory: Callable[[Summary], dict] | None = None) -> Summary:
        request = request or SummaryRequest()
        now = utc_now()
        summary = Summary(channel_id=channel_id, requested_by=request.user_id,
                          timeInterval=request.timeInterval, created_at=now, updated_at=now)
        document = _document(summary)
        if event_factory is not None:
            document["_request_delivery"] = {
                "event": event_factory(summary), "published": False, "received": False,
            }
        self.summaries.insert_one(document)
        return summary

    def pending_request_events(self, *, limit: int = 100) -> list[dict]:
        self._pagination(limit, 0)
        cursor = self.summaries.find({"_request_delivery.published": False}).sort(CHRONOLOGICAL).limit(limit)
        return [document["_request_delivery"]["event"] for document in cursor]

    def mark_request_published(self, summary_id: str) -> None:
        self.summaries.update_one({"id": summary_id}, {"$set": {"_request_delivery.published": True}})

    def receive_summary_request(self, event: dict) -> None:
        document = self.summaries.find_one({"id": event["subject_id"]})
        if document is None:
            raise SummaryNotFound()
        delivery = document.get("_request_delivery")
        if delivery is None or delivery["event"]["data"] != event["data"]:
            raise PersistenceConflict("summary.requested does not match the persisted request")
        self.summaries.update_one({"id": event["subject_id"]}, {"$set": {"_request_delivery.received": True}})

    def received_requests(self, *, limit: int = 100) -> list[Summary]:
        """Bandeja durable para el worker del paso 7; recibir no reclama el trabajo."""
        self._pagination(limit, 0)
        cursor = self.summaries.find({"_request_delivery.received": True,
                                      "messageState": MessageState.pendiente}).sort(CHRONOLOGICAL).limit(limit)
        return [_decode(Summary, document) for document in cursor]

    def get_summary(self, summary_id: str) -> Summary:
        summary = _decode(Summary, self.summaries.find_one({"id": summary_id}))
        if summary is None:
            raise SummaryNotFound()
        return summary

    def list_summaries(self, channel_id: str, *, limit: int = 100, offset: int = 0) -> list[Summary]:
        self._pagination(limit, offset)
        cursor = self.summaries.find({"channel_id": channel_id}).sort(NEWEST).skip(offset).limit(limit)
        return [_decode(Summary, document) for document in cursor]

    def latest_summary(self, channel_id: str) -> Summary:
        summary = _decode(Summary, self.summaries.find_one(
            {"channel_id": channel_id, "messageState": MessageState.completada}, sort=NEWEST))
        if summary is None:
            raise SummaryNotFound()
        return summary

    def start_summary(self, summary_id: str) -> Summary:
        """Compare-and-set: solo un worker puede reclamar pendiente → procesando."""
        return self._transition(summary_id, [MessageState.pendiente], MessageState.procesando)

    def complete_summary(self, summary_id: str) -> Summary:
        """Primero se guarda el digest; después se confirma el resultado público."""
        digest = self.get_digest_for_summary(summary_id)
        if digest is None:
            self.get_summary(summary_id)
            raise PersistenceConflict("A persisted digest is required to complete a summary")
        return self._transition(summary_id, [MessageState.procesando], MessageState.completada,
                                content=digest.content, error=None)

    def fail_summary(self, summary_id: str, error: str) -> Summary:
        if not error or not error.strip():
            raise ValueError("A failure needs a non-empty error")
        return self._transition(summary_id, [MessageState.pendiente, MessageState.procesando],
                                MessageState.fallida, error=error, content=None)

    def _transition(self, summary_id: str, expected: list[MessageState], target: MessageState,
                    **values) -> Summary:
        # $max evita retroceder updated_at incluso si el reloj local se corrige.
        document = self.summaries.find_one_and_update(
            {"id": summary_id, "messageState": {"$in": expected}},
            {"$set": {"messageState": target, **values}, "$max": {"updated_at": utc_now()}},
            return_document=ReturnDocument.AFTER)
        if document is not None:
            return _decode(Summary, document)
        current = self.get_summary(summary_id)
        if target in (MessageState.completada, MessageState.fallida):
            if current.messageState == target and all(getattr(current, key) == val for key, val in values.items()):
                return current
        raise SummaryStateConflict(f"Cannot move summary from {current.messageState.value} to {target.value}")

    def save_digest(self, digest: Digest) -> Digest:
        summary = self.get_summary(digest.summary_id)
        if summary.channel_id != digest.channel_id:
            raise PersistenceConflict("Digest and summary must belong to the same channel")
        if summary.timeInterval is not None and summary.timeInterval != digest.timeInterval:
            raise PersistenceConflict("Digest must use the requested timeInterval")
        window = self.get_window(summary.id)
        if window is not None:
            ids = window["message_ids"]
            if (not ids or digest.message_count != len(ids) or digest.first_message_id != ids[0]
                    or digest.last_message_id != ids[-1]
                    or digest.timeInterval != TimeInterval.model_validate(window["timeInterval"])):
                raise PersistenceConflict("Digest does not match the persisted message window")
            last_message = self.get_message(ids[-1])
            if last_message is None or digest.last_message_at != last_message.created_at:
                raise PersistenceConflict("Digest cursor does not match its last message")
        existing = self.get_digest_for_summary(digest.summary_id)
        if existing is not None:
            if existing.model_dump(exclude={"id", "created_at"}) == digest.model_dump(exclude={"id", "created_at"}):
                return existing
            raise PersistenceConflict("A different digest already exists for this summary")
        if summary.messageState != MessageState.procesando:
            raise SummaryStateConflict("A digest can only be saved while processing a summary")
        try:
            self.digests.insert_one(_document(digest))
        except DuplicateKeyError as exc:
            existing = self.get_digest_for_summary(digest.summary_id)
            if existing is not None and existing.model_dump(exclude={"id", "created_at"}) == digest.model_dump(exclude={"id", "created_at"}):
                return existing
            raise PersistenceConflict("Digest ID or summary already exists") from exc
        return digest

    def get_digest(self, digest_id: str) -> Digest | None:
        return _decode(Digest, self.digests.find_one({"id": digest_id}))

    def get_digest_for_summary(self, summary_id: str) -> Digest | None:
        return _decode(Digest, self.digests.find_one({"summary_id": summary_id}))

    def upsert_message(self, message: LocalMessage) -> bool:
        """Insertar una vez por ID. Un replay no modifica el mensaje ni infla contadores."""
        try:
            known_cursor = self.checkpoints.find_one(
                {"channel_id": message.channel_id, "last_message_id": message.id}, {"_id": 1})
            document = _document(message) | {"_history": {"covered": known_cursor is not None}}
            result = self.messages.update_one(
                {"id": message.id}, {"$setOnInsert": document}, upsert=True)
        except DuplicateKeyError:
            result = None
        if result is not None and result.upserted_id is not None:
            return True
        existing = self.get_message(message.id)
        if existing != message:
            raise PersistenceConflict("A different message already exists for this ID")
        return False

    def get_message(self, message_id: str) -> LocalMessage | None:
        return _decode(LocalMessage, self.messages.find_one({"id": message_id}))

    def list_messages(self, channel_id: str, time_interval: TimeInterval | None = None,
                      *, after: Checkpoint | None = None, limit: int = 1000) -> list[LocalMessage]:
        self._pagination(limit, 0)
        query = self._message_query(channel_id, time_interval, after)
        return [_decode(LocalMessage, doc) for doc in self.messages.find(query).sort(CHRONOLOGICAL).limit(limit)]

    def count_messages(self, channel_id: str, time_interval: TimeInterval | None = None,
                       *, after: Checkpoint | None = None) -> int:
        return self.messages.count_documents(self._message_query(channel_id, time_interval, after))

    @staticmethod
    def _message_query(channel_id: str, time_interval: TimeInterval | None,
                       after: Checkpoint | None) -> dict:
        query = {"channel_id": channel_id}
        if time_interval is not None:
            query["created_at"] = {"$gte": time_interval.start, "$lt": time_interval.end}
        if after is not None:
            if after.channel_id != channel_id:
                raise ValueError("A message cursor must belong to the queried channel")
            query.update(_after_cursor(after))
        return query

    def get_checkpoint(self, channel_id: str) -> Checkpoint:
        checkpoint = _decode(Checkpoint, self.checkpoints.find_one({"channel_id": channel_id}))
        checkpoint = checkpoint or Checkpoint(channel_id=channel_id)
        return Checkpoint(**checkpoint.model_dump(exclude={"messages_since"}),
                          messages_since=self.messages.count_documents(self._pending_query(checkpoint)))

    @staticmethod
    def _pending_query(checkpoint: Checkpoint) -> dict:
        # Documentos anteriores al paso 4 conservan la semántica del cursor hasta reconciliarse.
        legacy = {"_history": {"$exists": False}, **_after_cursor(checkpoint)}
        return {"channel_id": checkpoint.channel_id,
                "$or": [{"_history.covered": False}, legacy]}

    def list_pending_messages(self, channel_id: str, before, *, after: Checkpoint | None = None,
                               limit: int = 1000) -> list[LocalMessage]:
        self._pagination(limit, 0)
        checkpoint = self.get_checkpoint(channel_id)
        query = self._pending_query(checkpoint)
        query["created_at"] = {"$lt": before}
        if after is not None:
            if after.channel_id != channel_id:
                raise ValueError("A message cursor must belong to the queried channel")
            query["$and"] = [_after_cursor(after)]
        return [_decode(LocalMessage, doc) for doc in self.messages.find(query).sort(CHRONOLOGICAL).limit(limit)]

    def get_window(self, summary_id: str) -> dict | None:
        document = self.summaries.find_one({"id": summary_id}, {"_window": 1})
        if document is None:
            raise SummaryNotFound()
        return document.get("_window")

    def save_window(self, summary_id: str, message_ids: list[str], interval: TimeInterval | None) -> dict:
        window = {"message_ids": message_ids,
                  "timeInterval": interval.model_dump(mode="python") if interval else None}
        document = self.summaries.find_one_and_update(
            {"id": summary_id, "_window": {"$exists": False},
             "messageState": {"$in": [MessageState.pendiente, MessageState.procesando]}},
            {"$set": {"_window": window}}, return_document=ReturnDocument.AFTER)
        if document is not None:
            return document["_window"]
        existing = self.get_window(summary_id)
        if existing is None:
            raise SummaryStateConflict("Cannot build a new window for a finished summary")
        return existing

    def advance_checkpoint(self, summary_id: str) -> Checkpoint:
        """Confirmar solo un resumen incremental completado; jamás retroceder el cursor."""
        summary = self.get_summary(summary_id)
        if summary.messageState != MessageState.completada:
            raise SummaryStateConflict("Only a completed summary can advance a checkpoint")
        if summary.timeInterval is not None:
            raise PersistenceConflict("An on-demand timeInterval must not advance the channel checkpoint")
        digest = self.get_digest_for_summary(summary_id)
        if digest is None:
            raise PersistenceConflict("A persisted digest is required to advance a checkpoint")
        channel_id = summary.channel_id
        window = self.get_window(summary_id)
        # Sin snapshot legado solo se conoce con certeza la cobertura de los extremos del digest.
        ids = window["message_ids"] if window is not None else [digest.first_message_id, digest.last_message_id]
        for offset in range(0, len(ids), 1000):
            self.messages.update_many({"channel_id": channel_id, "id": {"$in": ids[offset:offset + 1000]}},
                                      {"$set": {"_history.covered": True}})
        initial = Checkpoint(channel_id=channel_id).model_dump(exclude={"messages_since"})
        try:
            self.checkpoints.update_one({"channel_id": channel_id}, {"$setOnInsert": initial}, upsert=True)
        except DuplicateKeyError:
            pass
        self.checkpoints.update_one(
            {"channel_id": channel_id, "$or": [
                {"last_message_at": None},
                {"last_message_at": {"$lt": digest.last_message_at}},
                {"last_message_at": digest.last_message_at, "last_message_id": {"$lt": digest.last_message_id}},
            ]},
            {"$set": {"last_message_at": digest.last_message_at,
                      "last_message_id": digest.last_message_id, "last_summary_id": summary_id}})
        return self.get_checkpoint(channel_id)

    @staticmethod
    def _pagination(limit: int, offset: int) -> None:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset non-negative")


def get_repository(request: Request) -> MongoRepository:
    """Dependencia preparada para rutas y servicios de las siguientes etapas."""
    return request.app.state.repository


RepositoryDependency = Annotated[MongoRepository, Depends(get_repository)]
