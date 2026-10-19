"""Proveedores de LLM detrás de un contrato interno único (`LLMRequest` → texto).

El resto del servicio solo conoce `LLMProvider`. `mock` sirve para desarrollo y pruebas;
`gemini` se conecta al final del paso 7 y no se invoca en pruebas automáticas.
"""
from dataclasses import dataclass
from typing import Protocol

from .. import config

PROMPT = (
    "Resume la conversación en español. Prioriza las decisiones tomadas, los eventos y las "
    "tareas asignadas con sus responsables. Usa solo los mensajes proporcionados y no inventes "
    "información. Si no hay decisiones o tareas, dilo."
)


class LLMError(Exception):
    """Fallo del proveedor. `retryable` indica si tiene sentido reintentar más tarde."""

    def __init__(self, reason: str, *, retryable: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


@dataclass(frozen=True)
class LLMRequest:
    summary_id: str
    channel_id: str
    messages: list[dict]  # id, author_id, author_name, content, created_at (ISO8601)
    prompt: str = PROMPT


class LLMProvider(Protocol):
    def generate(self, request: LLMRequest) -> str: ...


class MockProvider:
    """Resumen determinista y local. Marcas en el contenido simulan fallos:

    `[mock:fail]` → error definitivo; `[mock:retry]` → error transitorio.
    """

    def generate(self, request: LLMRequest) -> str:
        text = " ".join(message["content"] for message in request.messages)
        if "[mock:fail]" in text:
            raise LLMError("El proveedor simulado rechazó la solicitud")
        if "[mock:retry]" in text:
            raise LLMError("El proveedor simulado no está disponible", retryable=True)
        authors = sorted({message["author_name"] for message in request.messages})
        first, last = request.messages[0], request.messages[-1]
        return (f"[Resumen simulado] {len(request.messages)} mensajes de {', '.join(authors)} "
                f"entre {first['created_at']} y {last['created_at']}. "
                f"Último mensaje: {last['content'][:120]}")


def build_provider(name: str | None = None) -> LLMProvider:
    name = config.LLM_PROVIDER if name is None else name
    if name == "mock":
        return MockProvider()
    if name == "gemini":
        raise ValueError("LLM_PROVIDER=gemini aún no está implementado; usa LLM_PROVIDER=mock")
    raise ValueError(f"LLM_PROVIDER desconocido: {name!r}")
