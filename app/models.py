"""Schemas del dominio: summary, checkpoint y messageState."""
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class MessageState(str, Enum):
    pendiente = "pendiente"
    procesando = "procesando"
    completada = "completada"
    fallida = "fallida"


class TimeInterval(BaseModel):
    start: datetime
    end: datetime


class SummaryRequest(BaseModel):
    user_id: str | None = None
    timeInterval: TimeInterval | None = None


class Summary(BaseModel):
    id: str
    channel_id: str
    requested_by: str | None = None
    messageState: MessageState
    timeInterval: TimeInterval | None = None
    content: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class SummaryAccepted(BaseModel):
    summary_id: str
    messageState: MessageState = MessageState.pendiente


class Checkpoint(BaseModel):
    channel_id: str
    last_message_id: str | None = None
    last_message_at: datetime | None = None
    messages_since: int = Field(default=0, ge=0)
