"""Ventanas del historial y orquestación de solicitudes; sin llamadas al LLM."""
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Callable

from fastapi import Depends, Request

from . import config
from .clients.core_client import CoreClient, CoreClientDependency
from .errors import PersistenceConflict
from .models import Checkpoint, LocalMessage, Summary, SummaryRequest, TimeInterval, make_event
from .repository import MongoRepository, RepositoryDependency


@dataclass(frozen=True)
class MessageWindow:
    messages: tuple[LocalMessage, ...]
    time_interval: TimeInterval | None
    checkpoint: Checkpoint
    omitted: int = 0


class SummaryService:
    def __init__(self, repository: MongoRepository, core: CoreClient,
                 on_request_created: Callable[[], None] | None = None):
        self.repository = repository
        self.core = core
        # Despierta la publicación de la outbox sin esperar al sondeo de respaldo.
        self.on_request_created = on_request_created or (lambda: None)

    def request_summary(self, channel_id: str, request: SummaryRequest | None = None) -> Summary:
        self.core.get_channel(channel_id)
        request = request or SummaryRequest()
        if request.user_id is not None:
            self.core.get_user(request.user_id)

        def event_for(item: Summary) -> dict:
            return make_event(
                "summary.requested", item.id,
                {"channel_id": item.channel_id, "requested_by": item.requested_by,
                 "timeInterval": item.timeInterval.model_dump(mode="json") if item.timeInterval else None},
                occurred_at=item.created_at)

        summary = self.repository.create_summary(channel_id, request, event_factory=event_for)
        self.on_request_created()
        return summary

    def list_summaries(self, channel_id: str, *, limit: int = 100, offset: int = 0) -> list[Summary]:
        self.core.get_channel(channel_id)
        return self.repository.list_summaries(channel_id, limit=limit, offset=offset)

    def latest_summary(self, channel_id: str) -> Summary:
        self.core.get_channel(channel_id)
        return self.repository.latest_summary(channel_id)

    def build_window(self, summary_id: str, *, page_size: int = 1000) -> MessageWindow:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        summary = self.repository.get_summary(summary_id)
        snapshot = self.repository.get_window(summary_id)
        if snapshot is not None:
            return self._window_from_snapshot(summary.channel_id, snapshot)
        self.core.get_channel(summary.channel_id)
        # messages no publica todos los eventos si su broker falla: reconciliar siempre por API.
        self.repository.upsert_messages(
            [LocalMessage.model_validate(data) for data in self.core.list_messages(summary.channel_id)])
        interval = summary.timeInterval
        after = None
        messages = []
        while True:
            if interval:
                page = self.repository.list_messages(summary.channel_id, interval, after=after, limit=page_size)
            else:
                page = self.repository.list_pending_messages(summary.channel_id, summary.created_at,
                                                             after=after, limit=page_size)
            messages.extend(page)
            if len(page) < page_size:
                break
            last = page[-1]
            after = Checkpoint(channel_id=summary.channel_id,
                               last_message_id=last.id, last_message_at=last.created_at)
        messages, omitted = limit_window(messages, interval.end if interval else summary.created_at)
        if interval is None and messages:
            interval = TimeInterval(start=messages[0].created_at, end=summary.created_at)
        snapshot = self.repository.save_window(summary.id, [message.id for message in messages], interval,
                                               [message.id for message in omitted])
        return self._window_from_snapshot(summary.channel_id, snapshot)

    def _window_from_snapshot(self, channel_id: str, snapshot: dict) -> MessageWindow:
        messages = tuple(self.repository.get_message(message_id) for message_id in snapshot["message_ids"])
        if any(message is None or message.channel_id != channel_id for message in messages):
            raise PersistenceConflict("Persisted window contains missing or unrelated messages")
        interval = TimeInterval.model_validate(snapshot["timeInterval"]) if snapshot["timeInterval"] else None
        return MessageWindow(messages, interval, self.repository.get_checkpoint(channel_id),
                             len(snapshot.get("omitted_ids", [])))


def limit_window(messages: list[LocalMessage], end) -> tuple[list[LocalMessage], list[LocalMessage]]:
    """Aplicar los límites del LLM desde los mensajes más recientes: antigüedad, cantidad y tamaño.

    Devuelve (conservados, omitidos), ambos en orden cronológico. Conserva siempre el más reciente
    salvo que todos superen la antigüedad máxima.
    """
    cutoff = end - timedelta(days=config.LLM_MAX_DAYS)
    kept: list[LocalMessage] = []
    chars = 0
    for message in reversed(messages):
        if message.created_at < cutoff or len(kept) >= config.LLM_MAX_MESSAGES:
            break
        if kept and chars + len(message.content) > config.LLM_MAX_INPUT_CHARS:
            break
        kept.append(message)
        chars += len(message.content)
    kept.reverse()
    return kept, messages[:len(messages) - len(kept)]


def get_summary_service(request: Request, repository: RepositoryDependency,
                        core: CoreClientDependency) -> SummaryService:
    broker = getattr(request.app.state, "broker", None)
    return SummaryService(repository, core, broker.notify if broker is not None else None)


SummaryServiceDependency = Annotated[SummaryService, Depends(get_summary_service)]
