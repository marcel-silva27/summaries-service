"""Persistencia propia con PyMongo síncrono; las operaciones son atómicas por documento.

Invocar desde endpoints def o desde un hilo si el llamador es asíncrono.
La orquestación y la publicación de eventos pertenecen a las siguientes etapas.
"""
from datetime import datetime, timedelta
from typing import Annotated, Callable, NamedTuple, TypeVar

from bson.codec_options import CodecOptions
from fastapi import Depends, Request
from pymongo import ASCENDING, DESCENDING, ReturnDocument, UpdateOne
from pymongo.database import Database
from pymongo.errors import BulkWriteError, DuplicateKeyError

from .errors import PersistenceConflict, SummaryNotFound, SummaryStateConflict
from .models import (
    ChannelEntry, Checkpoint, Digest, DomainModel, LocalMessage, MessageState, Summary,
    SummaryRequest, TimeInterval, make_event, utc_now,
)

Model = TypeVar("Model", bound=DomainModel)
NEWEST = [("created_at", DESCENDING), ("id", DESCENDING)]
CHRONOLOGICAL = [("created_at", ASCENDING), ("id", ASCENDING)]


def _decode(model: type[Model], document: dict | None) -> Model | None:
    if document is None:
        return None
    return model.model_validate({key: value for key, value in document.items()
                                if key not in ("_id", "_request_delivery", "_history", "_window",
                                               "_lease", "_attempts", "_coverage_pending",
                                               "_terminal_delivery", "_entry_id", "_automatic_active")})


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


class Claim(NamedTuple):
    summary: Summary
    attempts: int


class MongoRepository:
    def __init__(self, database: Database):
        self.database = database.with_options(codec_options=CodecOptions(tz_aware=True))
        self.summaries = self.database["summaries"]
        self.digests = self.database["digests"]
        self.checkpoints = self.database["checkpoints"]
        self.messages = self.database["messages"]
        self.entries = self.database["entries"]

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
        self.summaries.create_index(
            [("_terminal_delivery.published", ASCENDING), ("updated_at", ASCENDING)],
            name="terminal_delivery_pending")
        # Solicitudes automáticas: una por entrada (idempotencia) y una activa por canal (exclusión).
        self.summaries.create_index(
            [("_entry_id", ASCENDING)], unique=True, name="summary_per_entry",
            partialFilterExpression={"_entry_id": {"$type": "string"}})
        self.summaries.create_index(
            [("channel_id", ASCENDING)], unique=True, name="one_active_automatic_per_channel",
            partialFilterExpression={"_automatic_active": True})
        self.entries.create_index("entry_id", unique=True, name="entry_id_unique")
        self.entries.create_index([("status", ASCENDING), ("received_at", ASCENDING)], name="entries_pending")
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
                       event_factory: Callable[[Summary], dict] | None = None,
                       entry_id: str | None = None) -> Summary:
        request = request or SummaryRequest()
        now = utc_now()
        summary = Summary(channel_id=channel_id, requested_by=request.user_id,
                          timeInterval=request.timeInterval, created_at=now, updated_at=now)
        document = _document(summary)
        if event_factory is not None:
            document["_request_delivery"] = {
                "event": event_factory(summary), "published": False, "received": False,
                "attempts": 0, "last_error": None,
            }
        if entry_id is not None:
            # Solicitud automática: única por entrada y, mientras no termine, única por canal.
            document["_entry_id"] = entry_id
            document["_automatic_active"] = True
        self.summaries.insert_one(document)
        return summary

    def create_automatic_summary(self, channel_id: str, user_id: str, entry_id: str, *,
                                 event_factory: Callable[[Summary], dict]) -> tuple[str, Summary | None]:
        """Crear la solicitud automática de una entrada. Devuelve (resultado, resumen).

        "requested": creada, o ya existía para esta entrada (reintento idempotente).
        "already_active": el canal ya tiene una solicitud automática sin terminar.
        """
        try:
            return "requested", self.create_summary(channel_id, SummaryRequest(user_id=user_id),
                                                    event_factory=event_factory, entry_id=entry_id)
        except DuplicateKeyError as exc:
            if "_entry_id" in (exc.details or {}).get("keyPattern", {}):
                return "requested", _decode(Summary, self.summaries.find_one({"_entry_id": entry_id}))
            return "already_active", None

    # --- Entradas a canales (inbox durable; las evalúa el worker) ---

    def receive_channel_entry(self, entry: ChannelEntry) -> bool:
        """Guardar una vez por entry_id; un reintento del mismo evento no crea otra entrada."""
        document = {**entry.model_dump(), "status": "pending", "attempts": 0, "outcome": None,
                    "summary_id": None, "received_at": utc_now()}
        result = self.entries.update_one({"entry_id": entry.entry_id}, {"$setOnInsert": document}, upsert=True)
        if result.upserted_id is not None:
            return True
        existing = self.entries.find_one({"entry_id": entry.entry_id})
        if (existing["channel_id"], existing["user_id"]) != (entry.channel_id, entry.user_id):
            raise PersistenceConflict("A different channel entry already exists for this entry_id")
        return False

    def claim_entry(self, owner: str, lease_seconds: float) -> dict | None:
        now = utc_now()
        return self.entries.find_one_and_update(
            {"$or": [{"status": "pending"}, {"status": "processing", "lease.expires_at": {"$lt": now}}]},
            {"$set": {"status": "processing",
                      "lease": {"owner": owner, "expires_at": now + timedelta(seconds=lease_seconds)}},
             "$inc": {"attempts": 1}},
            sort=[("received_at", ASCENDING), ("entry_id", ASCENDING)], projection={"_id": 0},
            return_document=ReturnDocument.AFTER)

    def release_entry(self, entry_id: str, owner: str) -> bool:
        return self.entries.update_one({"entry_id": entry_id, "status": "processing", "lease.owner": owner},
                                       {"$set": {"status": "pending"}, "$unset": {"lease": ""}}).matched_count == 1

    def finish_entry(self, entry_id: str, owner: str, outcome: str, summary_id: str | None = None) -> bool:
        return self.entries.update_one(
            {"entry_id": entry_id, "status": "processing", "lease.owner": owner},
            {"$set": {"status": "done", "outcome": outcome, "summary_id": summary_id,
                      "processed_at": utc_now()}, "$unset": {"lease": ""}}).matched_count == 1

    def count_recent_pending(self, channel_id: str, since: datetime) -> int:
        """Mensajes sin cobertura creados desde `since` (los más viejos no entrarán a ningún resumen)."""
        checkpoint = _decode(Checkpoint, self.checkpoints.find_one({"channel_id": channel_id}))
        query = self._pending_query(checkpoint or Checkpoint(channel_id=channel_id))
        query["created_at"] = {"$gte": since}
        return self.messages.count_documents(query)

    # --- Outbox: el evento se guarda en el mismo documento que el cambio que lo origina ---

    def pending_request_events(self, *, limit: int = 100) -> list[dict]:
        return self._pending_events("_request_delivery", CHRONOLOGICAL, limit)

    def mark_request_published(self, summary_id: str) -> None:
        self._mark_published("_request_delivery", summary_id)

    def record_request_delivery_error(self, summary_id: str, error: str) -> None:
        self._record_delivery_error("_request_delivery", summary_id, error)

    def pending_terminal_events(self, *, limit: int = 100) -> list[dict]:
        """summary.generated / summary.failed aún no publicados, en orden de ocurrencia."""
        return self._pending_events("_terminal_delivery", [("updated_at", ASCENDING), ("id", ASCENDING)], limit)

    def mark_terminal_published(self, summary_id: str) -> None:
        self._mark_published("_terminal_delivery", summary_id)

    def record_terminal_delivery_error(self, summary_id: str, error: str) -> None:
        self._record_delivery_error("_terminal_delivery", summary_id, error)

    def _pending_events(self, field: str, order: list, limit: int) -> list[dict]:
        self._pagination(limit, 0)
        cursor = self.summaries.find({f"{field}.published": False}).sort(order).limit(limit)
        return [document[field]["event"] for document in cursor]

    def _mark_published(self, field: str, summary_id: str) -> None:
        self.summaries.update_one({"id": summary_id}, {"$set": {f"{field}.published": True, f"{field}.last_error": None},
                                                       "$inc": {f"{field}.attempts": 1}})

    def _record_delivery_error(self, field: str, summary_id: str, error: str) -> None:
        self.summaries.update_one({"id": summary_id}, {"$set": {f"{field}.last_error": error[:300]},
                                                       "$inc": {f"{field}.attempts": 1}})

    def list_events(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        """Eventos durables emitidos por el servicio (solicitudes y resultados), más antiguos primero."""
        self._pagination(limit, offset)
        fetch = limit + offset
        events = [d["_request_delivery"]["event"] for d in self.summaries.find(
            {"_request_delivery": {"$exists": True}}).sort(CHRONOLOGICAL).limit(fetch)]
        events += [d["_terminal_delivery"]["event"] for d in self.summaries.find(
            {"_terminal_delivery": {"$exists": True}}).sort([("updated_at", ASCENDING), ("id", ASCENDING)]).limit(fetch)]
        events.sort(key=lambda event: (datetime.fromisoformat(event["occurred_at"]), event["type"], event["subject_id"]))
        return events[offset:offset + limit]

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

    def claim_summary(self, owner: str, lease_seconds: float) -> Claim | None:
        """Reclamar la solicitud recibida más antigua: pendiente, o procesando con lease vencido.

        Cada reclamo cuenta un intento. Solo el dueño del lease puede confirmar el resultado.
        """
        now = utc_now()
        document = self.summaries.find_one_and_update(
            {"_request_delivery.received": True,
             "$or": [{"messageState": MessageState.pendiente},
                     {"messageState": MessageState.procesando, "_lease.expires_at": {"$lt": now}}]},
            {"$set": {"messageState": MessageState.procesando,
                      "_lease": {"owner": owner, "expires_at": now + timedelta(seconds=lease_seconds)}},
             "$inc": {"_attempts": 1}, "$max": {"updated_at": now}},
            sort=CHRONOLOGICAL, return_document=ReturnDocument.AFTER)
        if document is None:
            return None
        if document.get("timeInterval") is None:
            # Un resumen incremental debe confirmar cobertura aunque el proceso caiga tras completarlo.
            self.summaries.update_one({"id": document["id"], "_lease.owner": owner},
                                      {"$set": {"_coverage_pending": True}})
        return Claim(_decode(Summary, document), document["_attempts"])

    def renew_lease(self, summary_id: str, owner: str, lease_seconds: float) -> bool:
        result = self.summaries.update_one(
            {"id": summary_id, "messageState": MessageState.procesando, "_lease.owner": owner},
            {"$set": {"_lease.expires_at": utc_now() + timedelta(seconds=lease_seconds)}})
        return result.matched_count == 1

    def release_summary(self, summary_id: str, owner: str) -> bool:
        """Devolver a pendiente (falla transitoria); el intento ya quedó contado."""
        document = self.summaries.find_one_and_update(
            {"id": summary_id, "messageState": MessageState.procesando, "_lease.owner": owner},
            {"$set": {"messageState": MessageState.pendiente}, "$unset": {"_lease": ""},
             "$max": {"updated_at": utc_now()}})
        return document is not None

    def pending_coverage(self, *, limit: int = 100) -> list[Summary]:
        """Resúmenes incrementales completados cuya cobertura aún no se confirmó."""
        self._pagination(limit, 0)
        cursor = self.summaries.find({"messageState": MessageState.completada, "_coverage_pending": True,
                                      "timeInterval": None}).sort(CHRONOLOGICAL).limit(limit)
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

    def complete_summary(self, summary_id: str, owner: str | None = None) -> Summary:
        """Primero se guarda el digest; después se confirma el resultado público."""
        digest = self.get_digest_for_summary(summary_id)
        if digest is None:
            self.get_summary(summary_id)
            raise PersistenceConflict("A persisted digest is required to complete a summary")
        return self._transition(summary_id, [MessageState.procesando], MessageState.completada,
                                owner=owner, content=digest.content, error=None,
                                structured=digest.structured.model_dump() if digest.structured else None)

    def fail_summary(self, summary_id: str, error: str, owner: str | None = None) -> Summary:
        if not error or not error.strip():
            raise ValueError("A failure needs a non-empty error")
        return self._transition(summary_id, [MessageState.pendiente, MessageState.procesando],
                                MessageState.fallida, owner=owner, error=error, content=None, structured=None)

    def _transition(self, summary_id: str, expected: list[MessageState], target: MessageState,
                    *, owner: str | None = None, **values) -> Summary:
        # $max evita retroceder updated_at incluso si el reloj local se corrige.
        now = utc_now()
        query = {"id": summary_id, "messageState": {"$in": expected}}
        if owner is not None:
            query["_lease.owner"] = owner  # Un worker con lease vencido no confirma resultados.
        update = {"$set": {"messageState": target, **values}, "$max": {"updated_at": now}}
        if target in (MessageState.completada, MessageState.fallida):
            update["$unset"] = {"_lease": "", "_automatic_active": ""}  # Libera la exclusión del canal.
            # El evento terminal nace en la misma escritura que el estado: si hay estado, hay evento.
            update["$set"]["_terminal_delivery"] = {
                "event": self._terminal_event(self.get_summary(summary_id), target, values, now),
                "published": False, "attempts": 0, "last_error": None}
        document = self.summaries.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
        if document is not None:
            return _decode(Summary, document)
        current = self.get_summary(summary_id)
        if target in (MessageState.completada, MessageState.fallida):
            snapshot = current.model_dump(mode="python")
            if current.messageState == target and all(snapshot[key] == val for key, val in values.items()):
                return current
        raise SummaryStateConflict(f"Cannot move summary from {current.messageState.value} to {target.value}")

    @staticmethod
    def _terminal_event(summary: Summary, target: MessageState, values: dict, occurred_at: datetime) -> dict:
        data = {"summary_id": summary.id, "channel_id": summary.channel_id,
                "requested_by": summary.requested_by, "messageState": target.value}
        if target == MessageState.fallida:
            data["error"] = values["error"]
        event_type = "summary.generated" if target == MessageState.completada else "summary.failed"
        return make_event(event_type, summary.id, data, occurred_at=occurred_at)

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
                    or digest.omitted_count != len(window.get("omitted_ids", []))
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
        return self.upsert_messages([message]) == 1

    def upsert_messages(self, messages: list[LocalMessage], *, batch_size: int = 1000) -> int:
        """Insertar en lote con la semántica de upsert_message; devuelve cuántos eran nuevos.

        Un ID repetido con otro contenido levanta PersistenceConflict y nunca sobrescribe.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        unique: dict[str, LocalMessage] = {}
        for message in messages:
            if unique.setdefault(message.id, message) != message:
                raise PersistenceConflict("A different message already exists for this ID")
        items = list(unique.values())
        inserted = 0
        for offset in range(0, len(items), batch_size):
            inserted += self._upsert_batch(items[offset:offset + batch_size])
        return inserted

    def _upsert_batch(self, batch: list[LocalMessage]) -> int:
        channels = list({message.channel_id for message in batch})
        covered_ids = {document["last_message_id"] for document in self.checkpoints.find(
            {"channel_id": {"$in": channels}}, {"last_message_id": 1})}
        operations = [UpdateOne({"id": message.id},
                                {"$setOnInsert": _document(message) | {"_history": {"covered": message.id in covered_ids}}},
                                upsert=True) for message in batch]
        try:
            result = self.messages.bulk_write(operations, ordered=False)
            new_positions = set(result.upserted_ids)
        except BulkWriteError as exc:
            # Una carrera por el índice único solo significa que otro proceso insertó ese ID.
            if any(error["code"] != 11000 for error in exc.details["writeErrors"]):
                raise
            new_positions = {item["index"] for item in exc.details["upserted"]}
        known = [message for position, message in enumerate(batch) if position not in new_positions]
        if known:
            existing = {document["id"]: _decode(LocalMessage, document)
                        for document in self.messages.find({"id": {"$in": [message.id for message in known]}})}
            if any(existing.get(message.id) != message for message in known):
                raise PersistenceConflict("A different message already exists for this ID")
        return len(new_positions)

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

    def save_window(self, summary_id: str, message_ids: list[str], interval: TimeInterval | None,
                    omitted_ids: list[str] | None = None) -> dict:
        window = {"message_ids": message_ids, "omitted_ids": omitted_ids or [],
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
        # Los omitidos por los límites de ventana también se dan por cubiertos: si no, el umbral
        # automático volvería a dispararse por mensajes que nunca se resumirán.
        ids = (window["message_ids"] + window.get("omitted_ids", []) if window is not None
               else [digest.first_message_id, digest.last_message_id])
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
        self.summaries.update_one({"id": summary_id}, {"$unset": {"_coverage_pending": ""}})
        return self.get_checkpoint(channel_id)

    @staticmethod
    def _pagination(limit: int, offset: int) -> None:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset non-negative")


def get_repository(request: Request) -> MongoRepository:
    """Dependencia preparada para rutas y servicios de las siguientes etapas."""
    return request.app.state.repository


RepositoryDependency = Annotated[MongoRepository, Depends(get_repository)]
