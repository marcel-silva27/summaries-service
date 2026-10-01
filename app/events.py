"""Publicación y consumo de eventos (RabbitMQ). Stub: se implementa en los pasos 6-8."""
from datetime import datetime, timezone

# Espejo local de eventos emitidos, igual que `GET /api/v1/events` en el núcleo.
events: list[dict] = []


def publish(event_type: str, subject_id: str, data: dict) -> dict:
    event = {"type": event_type, "subject_id": subject_id,
             "occurred_at": datetime.now(timezone.utc).isoformat(),
             "data": data}
    events.append(event)
    # TODO: publicar en el exchange `summaries` (topic, durable) vía pika.
    return event
