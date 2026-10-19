"""Proveedores de LLM detrás de un contrato interno único (`LLMRequest` → texto).

El resto del servicio solo conoce `LLMProvider`. `mock` sirve para desarrollo y pruebas;
`gemini` usa la API REST de Google (`generateContent`) y nunca se invoca en pruebas automáticas.
"""
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import httpx

from .. import config

logger = logging.getLogger(__name__)

PROMPT = (
    "Resume la conversación en español. Prioriza las decisiones tomadas, los eventos y las "
    "tareas asignadas con sus responsables. Usa solo los mensajes proporcionados y no inventes "
    "información: no conviertas días relativos (\"el viernes\") en fechas ni deduzcas datos que no "
    "aparezcan. Si no hay decisiones o tareas, dilo. Responde solo con el resumen, sin introducción."
)


class LLMError(Exception):
    """Fallo del proveedor. `retryable` indica si tiene sentido reintentar más tarde."""

    def __init__(self, reason: str, *, retryable: bool = False, wait: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.wait = wait  # Segundos que el proveedor pide esperar, si lo indicó.


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


class GeminiProvider:
    """Gemini vía REST. Modelo principal y, opcionalmente, uno de respaldo (cuotas por modelo).

    - 429: un único reintento inmediato si el proveedor indica una espera corta; si persiste,
      se prueba el modelo de respaldo; si también falla, error `retryable` (el worker reintenta).
    - 5xx, timeouts y errores de red se tratan igual. 4xx (credencial, modelo, petición) y
      respuestas bloqueadas o truncadas son definitivos.
    - La credencial viaja solo en la cabecera y nunca aparece en mensajes de error.
    """

    def __init__(self, *, api_key: str, model: str, fallback_model: str = "", base_url: str,
                 timeout: float, max_retry_wait: float = 10.0,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not api_key.strip() or not model.strip():
            raise ValueError("LLM_PROVIDER=gemini requiere LLM_API_KEY y LLM_MODEL")
        self.models = [model.strip()] + ([fallback_model.strip()] if fallback_model.strip()
                                         and fallback_model.strip() != model.strip() else [])
        self.max_retry_wait = max_retry_wait
        self._sleep = sleep
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False,
                                  headers={"x-goog-api-key": api_key.strip(),
                                           "Content-Type": "application/json"})

    def generate(self, request: LLMRequest) -> str:
        last: LLMError | None = None
        for model in self.models:
            started = time.monotonic()
            try:
                text = self._call(model, request)
            except LLMError as error:
                # Una línea por intento: permite saber qué modelo respondió y cuánto tardó.
                logger.warning("Gemini %s failed after %.1fs (%s): %s", model, time.monotonic() - started,
                               "retryable" if error.retryable else "permanent", error.reason)
                if not error.retryable:
                    raise
                last = error
                continue
            logger.info("Gemini %s answered in %.1fs", model, time.monotonic() - started)
            return text
        raise last

    def _call(self, model: str, request: LLMRequest) -> str:
        for attempt in (1, 2):
            try:
                response = self._post(model, request)
            except LLMError as error:
                if (attempt == 1 and error.retryable and error.wait is not None
                        and error.wait <= self.max_retry_wait):
                    self._sleep(error.wait)
                    continue
                raise
            return self._text(response)
        raise AssertionError("unreachable")

    def _post(self, model: str, request: LLMRequest) -> dict:
        transcript = "\n".join(f"[{m['created_at']}] {m['author_name']}: {m['content']}"
                               for m in request.messages)
        body = {"systemInstruction": {"parts": [{"text": request.prompt}]},
                "contents": [{"role": "user", "parts": [{"text": transcript}]}],
                "generationConfig": {"temperature": 0.2}}
        try:
            response = self._http.post(f"{self._base_url}/models/{model}:generateContent", json=body)
        except httpx.TimeoutException as exc:
            raise LLMError(f"Tiempo de espera agotado con {model}", retryable=True) from exc
        except httpx.RequestError as exc:
            raise LLMError(f"No se pudo contactar al proveedor ({type(exc).__name__})", retryable=True) from exc
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise LLMError("El proveedor devolvió una respuesta que no es JSON") from exc
        raise self._http_error(model, response)

    @staticmethod
    def _http_error(model: str, response: httpx.Response) -> LLMError:
        status = response.status_code
        try:
            error = response.json().get("error", {})
            message = str(error.get("message", ""))[:200]
            details = error.get("details") or []
        except (ValueError, AttributeError):
            message, details = "", []
        if status == 429:
            wait = None
            for detail in details:
                found = (re.fullmatch(r"(\d+(?:\.\d+)?)s", str(detail.get("retryDelay", "")))
                         if isinstance(detail, dict) else None)
                if found:
                    wait = float(found.group(1))
            return LLMError(f"Cuota del proveedor agotada para {model}", retryable=True, wait=wait)
        if status >= 500:
            return LLMError(f"El proveedor falló con HTTP {status} ({model})", retryable=True)
        if status in (401, 403):
            return LLMError("Credencial del proveedor rechazada: revisa LLM_API_KEY")
        return LLMError(f"El proveedor rechazó la solicitud (HTTP {status}, {model}): {message}")

    @staticmethod
    def _text(data: dict) -> str:
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise LLMError(f"El proveedor bloqueó la solicitud ({block})")
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("El proveedor no devolvió candidatos")
        candidate = candidates[0]
        if candidate.get("finishReason") not in (None, "STOP"):
            raise LLMError(f"Respuesta incompleta o bloqueada ({candidate.get('finishReason')})")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought")).strip()
        if not text:
            raise LLMError("El proveedor devolvió un resumen vacío")
        return text


def build_provider(name: str | None = None) -> LLMProvider:
    name = config.LLM_PROVIDER if name is None else name
    if name == "mock":
        return MockProvider()
    if name == "gemini":
        return GeminiProvider(api_key=config.LLM_API_KEY, model=config.LLM_MODEL,
                              fallback_model=config.LLM_FALLBACK_MODEL, base_url=config.LLM_API_URL,
                              timeout=config.LLM_TIMEOUT_SECONDS,
                              max_retry_wait=config.LLM_RETRY_MAX_WAIT_SECONDS)
    raise ValueError(f"LLM_PROVIDER desconocido: {name!r}")
