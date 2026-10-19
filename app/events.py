"""RabbitMQ en un hilo propio: historial continuo y entrega durable de solicitudes."""
import json
import logging
from math import isfinite
from typing import Callable
from threading import Event, Thread
from time import monotonic

import pika
from pydantic import AwareDatetime, BaseModel, ConfigDict
from pymongo.errors import PyMongoError

from . import config
from .errors import ServiceError
from .models import ChannelEntry, Identifier, LocalMessage, make_event  # noqa: F401  (make_event se reexporta)
from .repository import MongoRepository

logger = logging.getLogger(__name__)


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Identifier
    subject_id: Identifier
    occurred_at: AwareDatetime
    data: dict


class EventBroker:
    def __init__(self, repository: MongoRepository, *, url: str | None = None,
                 queue_prefix: str = "summaries", on_request_received: Callable[[], None] | None = None):
        self.repository = repository
        # Despierta al worker cuando llega trabajo guardado de forma durable (solicitud o entrada).
        self.on_request_received = on_request_received or (lambda: None)
        self.url = config.BROKER_URL if url is None else url
        self.history_queue = f"{queue_prefix}_for_chat_messages_queue"
        self.requests_queue = f"{queue_prefix}_for_summary_requested_queue"
        self.entries_queue = f"{queue_prefix}_for_channel_entered_queue"
        self.errors_queue = f"{queue_prefix}_errors_queue"
        self.dead_exchange = f"{queue_prefix}.deadletters"
        self._stop = Event()
        self.ready = Event()
        self._wake = Event()
        self._thread: Thread | None = None
        self.connection_parameters()  # Validar configuración antes de crear el hilo.

    def connection_parameters(self):
        if any(not isfinite(value) or value <= 0
               for value in (config.BROKER_TIMEOUT_SECONDS, config.BROKER_RETRY_SECONDS,
                                         config.OUTBOX_POLL_SECONDS)):
            raise ValueError("Broker timeout, retry delay and outbox poll must be positive and finite")
        parameters = pika.URLParameters(self.url)
        parameters.connection_attempts = 1
        parameters.socket_timeout = config.BROKER_TIMEOUT_SECONDS
        parameters.stack_timeout = 2 * config.BROKER_TIMEOUT_SECONDS
        parameters.blocked_connection_timeout = config.BROKER_TIMEOUT_SECONDS
        parameters.heartbeat = 30
        return parameters

    def declare_topology(self, channel) -> None:
        # El núcleo declara chat NO durable: cambiar este valor rompe la integración.
        channel.exchange_declare(exchange=config.CHAT_EXCHANGE, exchange_type="topic", durable=False)
        channel.exchange_declare(exchange=config.SUMMARIES_EXCHANGE, exchange_type="topic", durable=True)
        channel.exchange_declare(exchange=config.GATEWAY_EXCHANGE, exchange_type="topic", durable=True)
        channel.exchange_declare(exchange=self.dead_exchange, exchange_type="topic", durable=True)
        channel.queue_declare(queue=self.errors_queue, durable=True)
        channel.queue_bind(queue=self.errors_queue, exchange=self.dead_exchange, routing_key="#")
        for queue, exchange, key in (
            (self.history_queue, config.CHAT_EXCHANGE, "chat.message.created"),
            (self.requests_queue, config.SUMMARIES_EXCHANGE, "summary.requested"),
            (self.entries_queue, config.GATEWAY_EXCHANGE, "chat.channel.entered"),
        ):
            channel.queue_declare(queue=queue, durable=True,
                                  arguments={"x-dead-letter-exchange": self.dead_exchange})
            channel.queue_bind(queue=queue, exchange=exchange, routing_key=key)

    def notify(self) -> None:
        """Avisar que hay eventos nuevos en la outbox; seguro desde cualquier hilo."""
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Broker already started")
        self._thread = Thread(target=self._run, name="summaries-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * config.BROKER_TIMEOUT_SECONDS + config.MONGODB_TIMEOUT_MS / 1000 + 3)
            if self._thread.is_alive():
                raise RuntimeError("Broker thread did not stop")

    def _run(self) -> None:
        while not self._stop.is_set():
            connection = None
            try:
                connection = pika.BlockingConnection(self.connection_parameters())
                channel = connection.channel()
                self.declare_topology(channel)
                channel.confirm_delivery()
                channel.basic_qos(prefetch_count=20)
                channel.basic_consume(queue=self.history_queue, auto_ack=False,
                                      on_message_callback=self._consume_history)
                channel.basic_consume(queue=self.requests_queue, auto_ack=False,
                                      on_message_callback=self._consume_request)
                channel.basic_consume(queue=self.entries_queue, auto_ack=False,
                                      on_message_callback=self._consume_entry)
                self.ready.set()
                last_flush = float("-inf")  # Tras conectar, publicar lo que haya quedado pendiente.
                while not self._stop.is_set():
                    if self._wake.is_set() or monotonic() - last_flush >= config.OUTBOX_POLL_SECONDS:
                        self._wake.clear()
                        last_flush = monotonic()
                        self._flush_outbox(channel)
                    connection.process_data_events(time_limit=0.2)
            except (pika.exceptions.AMQPError, PyMongoError, OSError):
                if not self._stop.is_set():
                    logger.warning("Event connection unavailable; retrying", exc_info=True)
            finally:
                self.ready.clear()
                if connection is not None and connection.is_open:
                    try:
                        connection.close()
                    except pika.exceptions.AMQPError:
                        pass
            self._stop.wait(config.BROKER_RETRY_SECONDS)

    def _flush_outbox(self, channel) -> None:
        """Publicar solicitudes y resultados pendientes. Entrega al menos una vez, con identidad estable.

        Las solicitudes tienen cola propia y se publican como `mandatory`. Los resultados
        (summary.generated / summary.failed) no: sus consumidores (p. ej. un Gateway) declaran su
        propia cola durable; sin suscriptores el evento no se pierde, queda en Mongo y en
        `GET /api/v1/events`.
        """
        for event, mark, record_error, mandatory in (
            *((e, self.repository.mark_request_published, self.repository.record_request_delivery_error, True)
              for e in self.repository.pending_request_events()),
            *((e, self.repository.mark_terminal_published, self.repository.record_terminal_delivery_error, False)
              for e in self.repository.pending_terminal_events()),
        ):
            if self._stop.is_set():
                return
            try:
                channel.basic_publish(
                    exchange=config.SUMMARIES_EXCHANGE, routing_key=event["type"],
                    body=json.dumps(event, ensure_ascii=False).encode("utf-8"), mandatory=mandatory,
                    properties=pika.BasicProperties(content_type="application/json", delivery_mode=2,
                                                     message_id=f"{event['type']}:{event['subject_id']}"))
            except pika.exceptions.AMQPError as error:
                record_error(event["subject_id"], f"{type(error).__name__}: {error}")
                raise
            mark(event["subject_id"])

    def _consume_history(self, channel, method, properties, body) -> None:
        self._consume(channel, method, body, "chat.message.created")

    def _consume_request(self, channel, method, properties, body) -> None:
        self._consume(channel, method, body, "summary.requested")

    def _consume_entry(self, channel, method, properties, body) -> None:
        self._consume(channel, method, body, "chat.channel.entered")

    def _consume(self, channel, method, body: bytes, expected_type: str) -> None:
        try:
            event = _Envelope.model_validate_json(body)
            if event.type != expected_type or method.routing_key != expected_type:
                raise ValueError("Unexpected event type or routing key")
            if expected_type == "chat.message.created":
                message = LocalMessage.model_validate(event.data)
                if message.id != event.subject_id:
                    raise ValueError("Event subject does not match message ID")
                self.repository.upsert_message(message)
            elif expected_type == "chat.channel.entered":
                entry = ChannelEntry.model_validate(event.data)
                if entry.channel_id != event.subject_id:
                    raise ValueError("Event subject does not match the entered channel")
                self.repository.receive_channel_entry(entry)
                self.on_request_received()
            else:
                self.repository.receive_summary_request(event.model_dump(mode="json"))
                self.on_request_received()
        except PyMongoError:
            # La entrega sigue pendiente; esperar evita un requeue continuo durante una caída.
            logger.warning("Storage unavailable while consuming event", exc_info=True)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            self._stop.wait(config.BROKER_RETRY_SECONDS)
        except (ValueError, ServiceError):
            logger.warning("Invalid or conflicting event rejected", exc_info=True)
            channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
        else:
            channel.basic_ack(delivery_tag=method.delivery_tag)
