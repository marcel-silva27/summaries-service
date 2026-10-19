"""Publica `chat.channel.entered` como lo haría el Gateway; sirve para probar el resumen automático.

Uso (dentro del contenedor):
    python -m app.tools.publish_channel_entered CHANNEL_ID USER_ID [ENTRY_ID]

Repetir el mismo ENTRY_ID simula un reintento del evento: no abre otra solicitud.
"""
import json
import sys
from uuid import uuid4

import pika

from .. import config
from ..models import make_event


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__)
        return 2
    channel_id, user_id = argv[0], argv[1]
    entry_id = argv[2] if len(argv) == 3 else str(uuid4())
    event = make_event("chat.channel.entered", channel_id,
                       {"channel_id": channel_id, "user_id": user_id, "entry_id": entry_id})
    connection = pika.BlockingConnection(pika.URLParameters(config.BROKER_URL))
    channel = connection.channel()
    channel.exchange_declare(exchange=config.GATEWAY_EXCHANGE, exchange_type="topic", durable=True)
    channel.basic_publish(exchange=config.GATEWAY_EXCHANGE, routing_key="chat.channel.entered",
                          body=json.dumps(event).encode("utf-8"),
                          properties=pika.BasicProperties(content_type="application/json", delivery_mode=2))
    connection.close()
    print(f"published chat.channel.entered entry_id={entry_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
