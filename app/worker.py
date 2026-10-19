"""Worker de generación: toma solicitudes recibidas, arma la ventana y llama al LLM.

Corre en su propio hilo, independiente del consumo de RabbitMQ. Se despierta por aviso
(`notify`) y, como respaldo, por sondeo. Un lease con dueño protege contra workers caídos o
vencidos: solo el dueño vigente puede completar o fallar una solicitud.
"""
import logging
from threading import Event, Thread
from typing import Callable
from uuid import uuid4

from pymongo.errors import PyMongoError

from . import config
from .clients.core_client import CoreClient
from .clients.llm_client import LLMError, LLMProvider, LLMRequest
from .errors import (
    ChannelNotFound, DependencyFailed, DependencyUnavailable, PersistenceConflict,
    SummaryStateConflict, UserNotFound,
)
from .models import Digest, Summary
from .repository import Claim, MongoRepository
from .services import MessageWindow, SummaryService

logger = logging.getLogger(__name__)

EMPTY_WINDOW = "No hay mensajes en la ventana solicitada"
ALL_TOO_OLD = "Todos los mensajes superan la antigüedad máxima de {days} días"
OMITTED_NOTE = "\n\nNota: se omitieron {count} mensajes anteriores por los límites de la ventana."


class _Unprocessable(Exception):
    """La solicitud no se puede resumir (no se reintenta): termina `fallida` con este motivo."""


class SummaryWorker:
    def __init__(self, repository: MongoRepository, core: CoreClient, provider: LLMProvider):
        self.repository = repository
        self.core = core
        self.provider = provider
        self.service = SummaryService(repository, core, lambda: self.on_result())
        self.owner = f"worker-{uuid4()}"
        self._names: dict[str, str] = {}
        self._wake = Event()
        self._stop = Event()
        self._thread: Thread | None = None
        # Avisa a la outbox que hay un evento terminal por publicar (lo conecta main.py).
        self.on_result: Callable[[], None] = lambda: None

    def notify(self) -> None:
        """Avisar que hay trabajo; seguro desde cualquier hilo."""
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Worker already started")
        self._thread = Thread(target=self._run, name="summaries-worker", daemon=True)
        self._thread.start()
        self.notify()  # Procesar lo que haya quedado pendiente antes de iniciar.

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=config.LLM_TIMEOUT_SECONDS + config.CORE_TIMEOUT_SECONDS + 5)
            if self._thread.is_alive():
                raise RuntimeError("Worker thread did not stop")

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(config.WORKER_POLL_SECONDS)
            self._wake.clear()
            try:
                self.drain()
            except PyMongoError:
                # El lease vence solo; la solicitud se reclama cuando Mongo vuelva.
                logger.warning("Storage unavailable in worker; retrying", exc_info=True)
                self._stop.wait(config.WORKER_RETRY_SECONDS)

    def drain(self) -> int:
        """Procesar todo el trabajo disponible; devuelve cuántas solicitudes se reclamaron."""
        for summary in self.repository.pending_coverage():
            self._confirm_coverage(summary)
        self._process_entries()
        processed = 0
        while not self._stop.is_set():
            claim = self.repository.claim_summary(self.owner, config.WORKER_LEASE_SECONDS)
            if claim is None:
                break
            processed += 1
            self._process(claim)
        return processed

    def _process_entries(self) -> None:
        """Evaluar las entradas a canales recibidas; una solicitud automática sigue el flujo normal."""
        while not self._stop.is_set():
            entry = self.repository.claim_entry(self.owner, config.WORKER_LEASE_SECONDS)
            if entry is None:
                return
            outcome, summary_id = None, None
            try:
                outcome, summary_id = self.service.handle_channel_entry(entry)
            except (ChannelNotFound, UserNotFound):
                outcome = "invalid_reference"
            except (DependencyUnavailable, DependencyFailed):
                if entry["attempts"] < config.WORKER_MAX_ATTEMPTS:
                    if self.repository.release_entry(entry["entry_id"], self.owner):
                        self._stop.wait(config.WORKER_RETRY_SECONDS)
                    continue
                outcome = "dependency_unavailable"
            except PyMongoError:
                raise
            except Exception:
                logger.exception("Unexpected error evaluating channel entry %s", entry["entry_id"])
                outcome = "error"
            self.repository.finish_entry(entry["entry_id"], self.owner, outcome, summary_id)

    def _confirm_coverage(self, summary: Summary) -> None:
        try:
            self.repository.advance_checkpoint(summary.id)
        except (SummaryStateConflict, PersistenceConflict):
            logger.warning("Could not confirm coverage for %s", summary.id, exc_info=True)

    def _process(self, claim: Claim) -> None:
        summary = claim.summary
        try:
            if claim.attempts > config.WORKER_MAX_ATTEMPTS:
                raise _Unprocessable("Se superó el máximo de intentos de generación")
            self._generate(summary)
        except _Unprocessable as error:
            self._fail(summary, str(error))
        except LLMError as error:
            if error.retryable:
                self._retry_or_fail(claim, error.reason)
            else:
                self._fail(summary, error.reason)
        except (ChannelNotFound, UserNotFound) as error:
            self._fail(summary, error.detail)
        except (DependencyUnavailable, DependencyFailed) as error:
            self._retry_or_fail(claim, error.detail)
        except (SummaryStateConflict, PersistenceConflict):
            # Otro worker tomó la solicitud o el resultado persistido difiere: no pisar nada.
            logger.warning("Lost or conflicting ownership of %s", summary.id, exc_info=True)
        except PyMongoError:
            raise
        except Exception:
            logger.exception("Unexpected error generating %s", summary.id)
            self._fail(summary, "Error interno al generar el resumen")

    def _retry_or_fail(self, claim: Claim, reason: str) -> None:
        if claim.attempts >= config.WORKER_MAX_ATTEMPTS:
            self._fail(claim.summary, f"{reason} (tras {claim.attempts} intentos)")
        elif self.repository.release_summary(claim.summary.id, self.owner):
            self._stop.wait(config.WORKER_RETRY_SECONDS)

    def _fail(self, summary: Summary, reason: str) -> None:
        try:
            self.repository.fail_summary(summary.id, reason, owner=self.owner)
        except SummaryStateConflict:
            logger.warning("Could not fail %s: no longer owned", summary.id)
        else:
            self.on_result()

    def _generate(self, summary: Summary) -> None:
        window = self.service.build_window(summary.id)
        # Si un intento anterior ya guardó el digest, se reutiliza sin volver a llamar al LLM.
        if self.repository.get_digest_for_summary(summary.id) is None:
            self._call_llm(summary, window)
        self._require_lease(summary)
        self.repository.complete_summary(summary.id, owner=self.owner)
        self.on_result()
        if summary.timeInterval is None:
            self.repository.advance_checkpoint(summary.id)

    def _call_llm(self, summary: Summary, window: MessageWindow) -> None:
        if not window.messages:
            reason = (ALL_TOO_OLD.format(days=config.LLM_MAX_DAYS) if window.omitted else EMPTY_WINDOW)
            raise _Unprocessable(reason)
        request = LLMRequest(
            summary_id=summary.id, channel_id=summary.channel_id,
            messages=[{"id": message.id, "author_id": message.author_id,
                       "author_name": self._author_name(message.author_id), "content": message.content,
                       "created_at": message.created_at.isoformat()} for message in window.messages])
        self._require_lease(summary)
        content = self.provider.generate(request)
        if not isinstance(content, str) or not content.strip():
            raise LLMError("El proveedor devolvió un resumen vacío")
        if window.omitted:
            content += OMITTED_NOTE.format(count=window.omitted)
        first, last = window.messages[0], window.messages[-1]
        self._require_lease(summary)
        self.repository.save_digest(Digest(
            summary_id=summary.id, channel_id=summary.channel_id, timeInterval=window.time_interval,
            content=content, message_count=len(window.messages), first_message_id=first.id,
            last_message_id=last.id, last_message_at=last.created_at, omitted_count=window.omitted))

    def _require_lease(self, summary: Summary) -> None:
        if not self.repository.renew_lease(summary.id, self.owner, config.WORKER_LEASE_SECONDS):
            raise SummaryStateConflict("The processing lease was lost")

    def _author_name(self, author_id: str) -> str:
        if author_id not in self._names:
            try:
                self._names[author_id] = self.core.get_user(author_id)["display_name"]
            except (UserNotFound, DependencyUnavailable, DependencyFailed):
                return author_id  # El nombre es solo contexto: no bloquea el resumen ni se cachea.
        return self._names[author_id]
