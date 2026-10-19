"""Modelos de respuesta que solo existen en la API HTTP (errores, eventos expuestos, salud)."""
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field


class ValidationErrorItem(BaseModel):
    loc: list[str | int] = Field(description="Dónde falló: `body`, `query` o `path` y el nombre del campo.")
    msg: str
    type: str


class ErrorResponse(BaseModel):
    """Formato uniforme de error. `detail` es un texto; en errores de validación de FastAPI es una lista."""
    detail: str | list[ValidationErrorItem] = Field(examples=["Channel not found"])


class EventOut(BaseModel):
    """Evento durable del servicio, con el sobre del curso. Los esquemas de `data` están en `docs/eventos/`."""
    type: str = Field(examples=["summary.generated"])
    subject_id: str = Field(description="ID del sujeto: el `summary_id` en los eventos `summary.*`.")
    occurred_at: AwareDatetime
    data: dict


class Health(BaseModel):
    status: Literal["ok"] = "ok"
