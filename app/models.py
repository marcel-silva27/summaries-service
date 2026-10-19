"""Contratos del dominio; BSON y ObjectId quedan fuera de estos modelos."""
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Self
from uuid import uuid4

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field,
    StringConstraints, field_validator, model_validator,
)


def utc_milliseconds(value: datetime) -> datetime:
    """Usar la misma precisión que BSON para cursores y ventanas reproducibles."""
    value = value.astimezone(timezone.utc)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def utc_now() -> datetime:
    return utc_milliseconds(datetime.now(timezone.utc))


UTCDateTime = Annotated[AwareDatetime, AfterValidator(utc_milliseconds)]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


def make_event(event_type: str, subject_id: str, data: dict, *, occurred_at: datetime | None = None) -> dict:
    """Evento con el sobre del curso: type, subject_id, occurred_at (ISO8601 UTC) y data."""
    return {"type": event_type, "subject_id": subject_id,
            "occurred_at": (occurred_at or utc_now()).isoformat(), "data": data}


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MessageState(str, Enum):
    pendiente = "pendiente"
    procesando = "procesando"
    completada = "completada"
    fallida = "fallida"


class TimeInterval(DomainModel):
    """Ventana UTC semiabierta: start <= created_at < end."""
    start: UTCDateTime = Field(description="Inicio (incluido). ISO 8601 con zona horaria.",
                               examples=["2026-10-19T09:00:00Z"])
    end: UTCDateTime = Field(description="Fin (excluido). Debe ser posterior a `start`.",
                             examples=["2026-10-19T18:00:00Z"])

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("timeInterval.start must be before timeInterval.end")
        return self


class SummaryEvent(DomainModel):
    """Evento mencionado en la conversación (reunión, entrega, hito...)."""
    description: NonEmptyText = Field(description="Qué ocurrirá u ocurrió.", examples=["Reunión con el profesor"])
    when: str | None = Field(default=None, description="Cuándo, tal como se dijo en la conversación; sin inventar fechas.",
                             examples=["el lunes a las 9:00"])


class SummaryTask(DomainModel):
    """Tarea asignada en la conversación."""
    description: NonEmptyText = Field(description="Qué hay que hacer.", examples=["Escribir el informe final"])
    assignee: str | None = Field(default=None, description="Responsable, si se indicó.", examples=["Sofía"])


class StructuredSummary(DomainModel):
    """Resumen estructurado: lo que devuelve el LLM (validado) y de lo que se deriva `content`."""
    overview: NonEmptyText = Field(description="Resumen general en una o dos frases.")
    decisions: list[NonEmptyText] = Field(default_factory=list, description="Decisiones tomadas.")
    events: list[SummaryEvent] = Field(default_factory=list, description="Eventos mencionados.")
    tasks: list[SummaryTask] = Field(default_factory=list, description="Tareas asignadas con su responsable.")

    @field_validator("overview")
    @classmethod
    def overview_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("overview cannot be blank")
        return value

    def render(self) -> str:
        """Texto legible del resumen; lo genera el código, no el LLM."""
        def section(title: str, lines: list[str]) -> list[str]:
            return ["", f"**{title}:**", *(lines or ["- Ninguna."])]
        return "\n".join([
            self.overview.strip(),
            *section("Decisiones tomadas", [f"- {item}" for item in self.decisions]),
            *section("Eventos", [f"- {item.description}" + (f" ({item.when})" if item.when else "")
                                 for item in self.events]),
            *section("Tareas asignadas", [f"- {item.description}"
                                          + (f" — Responsable: {item.assignee}" if item.assignee else "")
                                          for item in self.tasks]),
        ])


class SummaryRequest(DomainModel):
    """Cuerpo opcional del POST. Vacío = resumir lo nuevo desde el último resumen del canal."""
    user_id: Identifier | None = Field(default=None, description="Usuario que lo pide; si se indica, debe existir en `users`.",
                                       examples=["b64bb09f-7517-49c8-b701-58a5b8e297f5"])
    timeInterval: TimeInterval | None = Field(
        default=None, description="Acota los mensajes a resumir. Un resumen por intervalo no avanza el cursor del canal.")

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra={"examples": [
        {}, {"user_id": "b64bb09f-7517-49c8-b701-58a5b8e297f5"},
        {"timeInterval": {"start": "2026-10-19T09:00:00Z", "end": "2026-10-19T18:00:00Z"}}]})


class Summary(DomainModel):
    """Un resumen y su estado. Se consulta por polling hasta `completada` o `fallida`."""
    id: Identifier = Field(default_factory=lambda: str(uuid4()), description="ID del resumen (uuid).")
    channel_id: Identifier = Field(description="Canal resumido.")
    requested_by: Identifier | None = Field(default=None, description="Usuario que lo pidió; `null` si no se indicó.")
    messageState: MessageState = Field(default=MessageState.pendiente, description="Estado de la solicitud.")
    timeInterval: TimeInterval | None = Field(default=None, description="Intervalo pedido; `null` = incremental.")
    content: NonEmptyText | None = Field(default=None, description="Texto del resumen; solo si está `completada`.")
    structured: StructuredSummary | None = Field(
        default=None, description="Mismo resumen en campos (decisiones, eventos, tareas); solo si está completada.")
    error: NonEmptyText | None = Field(default=None, description="Motivo legible del fallo; solo si está `fallida`.")
    created_at: UTCDateTime = Field(default_factory=utc_now, description="Cuándo se pidió.")
    updated_at: UTCDateTime = Field(default_factory=utc_now, description="Último cambio de estado.")

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra={"examples": [{
        "id": "08ac19f0-b81f-413c-be12-416d99f2672d", "channel_id": "84e015c1-ed09-470c-b584-6f6210ea219c",
        "requested_by": None, "messageState": "completada", "timeInterval": None,
        "content": "Resumen general.\n\n**Decisiones tomadas:**\n- Entregar el viernes\n\n**Eventos:**\n- Ninguna.\n\n**Tareas asignadas:**\n- Hacer las pruebas — Responsable: Beto",
        "structured": {"overview": "Resumen general.", "decisions": ["Entregar el viernes"], "events": [],
                       "tasks": [{"description": "Hacer las pruebas", "assignee": "Beto"}]},
        "error": None, "created_at": "2026-10-19T05:14:58.123Z", "updated_at": "2026-10-19T05:15:00.456Z"}]})

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.messageState == MessageState.completada:
            if not self.content or not self.content.strip() or self.error is not None:
                raise ValueError("A completed summary needs content and no error")
        elif self.messageState == MessageState.fallida:
            if not self.error or not self.error.strip() or self.content is not None:
                raise ValueError("A failed summary needs an error and no content")
        elif self.content is not None or self.error is not None:
            raise ValueError("An unfinished summary cannot have content or error")
        if self.messageState != MessageState.completada and self.structured is not None:
            raise ValueError("Only a completed summary can have a structured result")
        return self


class SummaryAccepted(DomainModel):
    """Respuesta 202: la solicitud quedó guardada; el resumen se genera en segundo plano."""
    summary_id: Identifier = Field(description="Úsalo en `GET /api/v1/summaries/{summary_id}` hasta `completada` o `fallida`.")
    messageState: MessageState = Field(default=MessageState.pendiente, description="Siempre `pendiente` al aceptar.")

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra={"examples": [
        {"summary_id": "08ac19f0-b81f-413c-be12-416d99f2672d", "messageState": "pendiente"}]})


class Digest(DomainModel):
    """Resultado inmutable del LLM y trazabilidad de la ventana de una solicitud."""
    id: Identifier = Field(default_factory=lambda: str(uuid4()))
    summary_id: Identifier
    channel_id: Identifier
    timeInterval: TimeInterval
    content: NonEmptyText
    message_count: int = Field(ge=1)
    first_message_id: Identifier
    last_message_id: Identifier
    last_message_at: UTCDateTime
    structured: StructuredSummary | None = None
    # Mensajes dejados fuera por los límites de ventana (7 días / 500 mensajes / tamaño).
    omitted_count: int = Field(default=0, ge=0)
    created_at: UTCDateTime = Field(default_factory=utc_now)

    @field_validator("content")
    @classmethod
    def meaningful_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Digest content cannot be blank")
        return value

    @model_validator(mode="after")
    def cursor_in_window(self) -> Self:
        if not self.timeInterval.start <= self.last_message_at < self.timeInterval.end:
            raise ValueError("The last message must be inside timeInterval")
        if (self.message_count == 1) != (self.first_message_id == self.last_message_id):
            raise ValueError("First and last message IDs must agree with message_count")
        return self


class ChannelEntry(DomainModel):
    """Entrada de un usuario a un canal, publicada por el Gateway (`chat.channel.entered`)."""
    channel_id: Identifier
    user_id: Identifier
    entry_id: Identifier  # Identifica la entrada; se conserva al reintentar el evento.


class LocalMessage(DomainModel):
    """Proyección local del contrato público de messages; no es su fuente de verdad."""
    id: Identifier
    channel_id: Identifier
    author_id: Identifier
    content: str = Field(min_length=1, max_length=4000)
    created_at: UTCDateTime


class Checkpoint(DomainModel):
    """Cursor monotónico por canal; la cobertura incremental se registra por mensaje."""
    channel_id: Identifier
    last_message_id: Identifier | None = None
    last_message_at: UTCDateTime | None = None
    last_summary_id: Identifier | None = None
    # Proyección calculada al leer, nunca un contador que se incremente por evento.
    messages_since: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def complete_cursor(self) -> Self:
        if (self.last_message_id is None) != (self.last_message_at is None):
            raise ValueError("Checkpoint requires both last_message_id and last_message_at")
        if self.last_summary_id is not None and self.last_message_id is None:
            raise ValueError("A summary checkpoint requires a message cursor")
        return self
