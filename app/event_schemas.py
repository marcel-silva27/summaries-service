"""Esquemas de los eventos del broker (sobre del curso: type, subject_id, occurred_at, data).

Son la fuente de `docs/eventos/*.schema.json` (`python -m app.tools.export_event_schemas`) y las pruebas
validan contra ellos los eventos que el servicio emite de verdad.
"""
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .models import ChannelEntry, Identifier, LocalMessage, TimeInterval


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SummaryRequestedData(_Data):
    channel_id: Identifier
    requested_by: Identifier | None = Field(description="Quién lo pidió; `null` si no se indicó.")
    timeInterval: TimeInterval | None = Field(description="Intervalo pedido; `null` = incremental.")
    trigger: Literal["automatic"] | None = Field(
        default=None, description="Presente solo en solicitudes automáticas (entrada a un canal).")
    entry_id: Identifier | None = Field(default=None, description="Entrada que originó una solicitud automática.")


class _TerminalData(_Data):
    summary_id: Identifier
    channel_id: Identifier
    requested_by: Identifier | None = Field(description="Quién lo pidió; sirve para saber a quién avisar.")


class SummaryGeneratedData(_TerminalData):
    messageState: Literal["completada"]


class SummaryFailedData(_TerminalData):
    messageState: Literal["fallida"]
    error: str = Field(min_length=1, description="Motivo legible, sin secretos.")


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_id: Identifier
    occurred_at: AwareDatetime


class SummaryRequestedEvent(_Envelope):
    """Se publica al guardar una solicitud (manual o automática). Lo consume el propio servicio."""
    type: Literal["summary.requested"]
    data: SummaryRequestedData

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"type": "summary.requested", "subject_id": "08ac19f0-b81f-413c-be12-416d99f2672d",
         "occurred_at": "2026-10-19T05:14:58.123000+00:00",
         "data": {"channel_id": "84e015c1-ed09-470c-b584-6f6210ea219c", "requested_by": None, "timeInterval": None}},
        {"type": "summary.requested", "subject_id": "7ba720a7-8067-4329-bdf8-539abd0cc9af",
         "occurred_at": "2026-10-19T05:20:00+00:00",
         "data": {"channel_id": "84e015c1-ed09-470c-b584-6f6210ea219c", "requested_by": "b64bb09f-7517-49c8-b701-58a5b8e297f5",
                  "timeInterval": None, "trigger": "automatic", "entry_id": "entry-1"}}]})


class SummaryGeneratedEvent(_Envelope):
    """Se publica al completarse un resumen. El contenido se consulta con `GET /api/v1/summaries/{id}`."""
    type: Literal["summary.generated"]
    data: SummaryGeneratedData

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "type": "summary.generated", "subject_id": "08ac19f0-b81f-413c-be12-416d99f2672d",
        "occurred_at": "2026-10-19T05:15:00.456000+00:00",
        "data": {"summary_id": "08ac19f0-b81f-413c-be12-416d99f2672d", "channel_id": "84e015c1-ed09-470c-b584-6f6210ea219c",
                 "requested_by": None, "messageState": "completada"}}]})


class SummaryFailedEvent(_Envelope):
    """Se publica al fallar un resumen."""
    type: Literal["summary.failed"]
    data: SummaryFailedData

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "type": "summary.failed", "subject_id": "33214886-541a-4197-931d-e2ec5360c9f8",
        "occurred_at": "2026-10-19T05:44:11.329000+00:00",
        "data": {"summary_id": "33214886-541a-4197-931d-e2ec5360c9f8", "channel_id": "84e015c1-ed09-470c-b584-6f6210ea219c",
                 "requested_by": None, "messageState": "fallida", "error": "No hay mensajes en la ventana solicitada"}}]})


class ChannelEnteredEvent(_Envelope):
    """Lo publica el Gateway al entrar un usuario a un canal (exchange `gateway`). `subject_id` = `channel_id`."""
    type: Literal["chat.channel.entered"]
    data: ChannelEntry

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "type": "chat.channel.entered", "subject_id": "84e015c1-ed09-470c-b584-6f6210ea219c",
        "occurred_at": "2026-10-19T05:20:00+00:00",
        "data": {"channel_id": "84e015c1-ed09-470c-b584-6f6210ea219c", "user_id": "b64bb09f-7517-49c8-b701-58a5b8e297f5",
                 "entry_id": "entry-1"}}]})


class MessageCreatedEvent(_Envelope):
    """Lo publica el servicio `messages` del chat (exchange `chat`). Se consume para el historial local."""
    type: Literal["chat.message.created"]
    data: LocalMessage

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "type": "chat.message.created", "subject_id": "22f56d29-1466-4a8f-ad43-56d1cae6011b",
        "occurred_at": "2026-10-19T05:14:50.000000+00:00",
        "data": {"id": "22f56d29-1466-4a8f-ad43-56d1cae6011b", "channel_id": "84e015c1-ed09-470c-b584-6f6210ea219c",
                 "author_id": "b64bb09f-7517-49c8-b701-58a5b8e297f5", "content": "Decidimos entregar el viernes",
                 "created_at": "2026-10-19T05:14:50+00:00"}}]})


EVENTS = {model.model_fields["type"].annotation.__args__[0]: model for model in (
    SummaryRequestedEvent, SummaryGeneratedEvent, SummaryFailedEvent, ChannelEnteredEvent, MessageCreatedEvent)}
PUBLISHED = ("summary.requested", "summary.generated", "summary.failed")


def json_schema(event_type: str) -> dict:
    schema = EVENTS[event_type].model_json_schema()
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": event_type, **schema} | {"title": event_type}
