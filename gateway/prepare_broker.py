"""Cola de auditoría del entorno integrado para los eventos públicos de Polls."""
import os

import pika


def main():
    parameters = pika.URLParameters(os.environ["BROKER_URL"])
    parameters.connection_attempts = 30
    parameters.retry_delay = 1
    parameters.socket_timeout = 2
    parameters.stack_timeout = 5
    with pika.BlockingConnection(parameters) as connection:
        channel = connection.channel()
        channel.exchange_declare(exchange="polls.events", exchange_type="topic", durable=True)
        channel.queue_declare(queue="hilo.polls.events", durable=True)
        channel.queue_bind(queue="hilo.polls.events", exchange="polls.events", routing_key="poll.#")


if __name__ == "__main__":
    main()
