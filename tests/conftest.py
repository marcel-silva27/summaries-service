import os
from uuid import uuid4

import pytest
import httpx
import pika
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.repository import MongoRepository
from app.clients.core_client import CoreClient
from app.errors import register_handlers
from app.events import EventBroker
from app.routes.summaries import router


@pytest.fixture(scope="session")
def mongo_client():
    url = os.environ.get("TEST_MONGODB_URL")
    if not url:
        pytest.fail("Set TEST_MONGODB_URL to an isolated MongoDB 7, or use compose.test.yaml")
    with MongoClient(url, tz_aware=True, serverSelectionTimeoutMS=3000) as client:
        client.admin.command("ping")
        yield client


@pytest.fixture
def repository(mongo_client):
    # Nunca se toma el nombre de la base del entorno ni de datos del usuario.
    name = f"test_summaries_{uuid4().hex}"
    repository = MongoRepository(mongo_client[name])
    repository.ensure_indexes()
    try:
        yield repository
    finally:
        mongo_client.drop_database(name)


@pytest.fixture
def api(repository):
    def handler(request):
        resource = request.url.path.split("/")[-1]
        if resource == "missing":
            return httpx.Response(404)
        if resource == "unavailable":
            return httpx.Response(503)
        if request.url.host == "users":
            return httpx.Response(200, json={"id": resource, "display_name": "Ana"})
        if request.url.host == "channels":
            return httpx.Response(200, json={"id": resource, "name": "General"})
        return httpx.Response(200, json=[])

    test_app = FastAPI()
    register_handlers(test_app)
    test_app.include_router(router)
    test_app.state.repository = repository
    with CoreClient(users_url="http://users", channels_url="http://channels", messages_url="http://messages",
                    transport=httpx.MockTransport(handler)) as core:
        test_app.state.core_client = core
        with TestClient(test_app) as client:
            yield client


@pytest.fixture
def broker_url():
    url = os.environ.get("TEST_BROKER_URL")
    if not url:
        pytest.fail("Set TEST_BROKER_URL to an isolated RabbitMQ, or use compose.test.yaml")
    return url


@pytest.fixture
def broker(repository, broker_url):
    broker = EventBroker(repository, url=broker_url, queue_prefix=f"test_{uuid4().hex}")
    broker.start()
    try:
        assert broker.ready.wait(15), "RabbitMQ consumer did not become ready"
        yield broker
    finally:
        broker.stop()
        with pika.BlockingConnection(broker.connection_parameters()) as connection:
            channel = connection.channel()
            for queue in (broker.history_queue, broker.requests_queue, broker.errors_queue):
                channel.queue_delete(queue=queue)
            channel.exchange_delete(exchange=broker.dead_exchange)
