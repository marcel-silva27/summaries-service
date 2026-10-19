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


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MessageState(str, Enum):
    pendiente = "pendiente"
    procesando = "procesando"
    completada = "completada"
    fallida = "fallida"


class TimeInterval(DomainModel):
    """Ventana UTC semiabierta: start <= created_at < end."""
    start: UTCDateTime
    end: UTCDateTime

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("timeInterval.start must be before timeInterval.end")
        return self


class SummaryRequest(DomainModel):
    user_id: Identifier | None = None
    timeInterval: TimeInterval | None = None


class Summary(DomainModel):
    id: Identifier = Field(default_factory=lambda: str(uuid4()))
    channel_id: Identifier
    requested_by: Identifier | None = None
    messageState: MessageState = MessageState.pendiente
    timeInterval: TimeInterval | None = None
    content: NonEmptyText | None = None
    error: NonEmptyText | None = None
    created_at: UTCDateTime = Field(default_factory=utc_now)
    updated_at: UTCDateTime = Field(default_factory=utc_now)

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
        return self


class SummaryAccepted(DomainModel):
    summary_id: Identifier
    messageState: MessageState = MessageState.pendiente


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
