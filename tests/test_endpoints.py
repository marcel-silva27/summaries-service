"""Pruebas de servicio por endpoint (Unidad 7): al menos un caso válido y uno de error para cada uno.

El núcleo del chat se simula (fixture `api` en conftest.py: `missing` → 404, `unavailable` → 503) y la base
es una Mongo desechable por prueba (fixture `repository`). Cada endpoint tiene su sección.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from test_repository import completed

CHANNEL = "/api/v1/channels/channel"


@pytest.fixture
def main_api(repository):
    """La aplicación real (`app.main`) sin su lifespan: solo expone /health y /api/v1/events con la BD de prueba."""
    previous = getattr(app.state, "repository", None)
    app.state.repository = repository
    try:
        yield TestClient(app)  # Sin `with`: no se ejecuta el lifespan (ni Mongo real, ni RabbitMQ).
    finally:
        app.state.repository = previous


# --- POST /api/v1/channels/{channel_id}/summaries ---

def test_post_summary_returns_202_with_location_and_pending_state(api):
    response = api.post(f"{CHANNEL}/summaries", json={"user_id": "user"})
    assert response.status_code == 202
    body = response.json()
    assert body["messageState"] == "pendiente"
    assert response.headers["Location"] == f"/api/v1/summaries/{body['summary_id']}"


def test_post_summary_accepts_a_time_interval(api, repository):
    interval = {"start": "2026-10-18T12:00:00Z", "end": "2026-10-18T13:00:00Z"}
    response = api.post(f"{CHANNEL}/summaries", json={"timeInterval": interval})
    assert response.status_code == 202
    assert repository.get_summary(response.json()["summary_id"]).timeInterval.start.hour == 12


def test_post_summary_for_unknown_channel_returns_404(api, repository):
    response = api.post("/api/v1/channels/missing/summaries")
    assert response.status_code == 404
    assert repository.summaries.count_documents({}) == 0


def test_post_summary_for_unknown_user_returns_422(api):
    assert api.post(f"{CHANNEL}/summaries", json={"user_id": "missing"}).status_code == 422


def test_post_summary_with_core_down_returns_503(api):
    assert api.post("/api/v1/channels/unavailable/summaries").status_code == 503


@pytest.mark.parametrize("body", [{"timeInterval": {"start": "bad", "end": "bad"}}, {"unknown": True}])
def test_post_summary_with_invalid_body_returns_422(api, body):
    assert api.post(f"{CHANNEL}/summaries", json=body).status_code == 422


# --- GET /api/v1/summaries/{summary_id} ---

def test_get_summary_returns_a_completed_summary_with_its_content(api, repository):
    done = completed(repository)
    response = api.get(f"/api/v1/summaries/{done.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == done.id and body["messageState"] == "completada"
    assert body["content"]


def test_get_summary_exposes_the_error_of_a_failed_summary(api, repository):
    summary = repository.create_summary("channel")
    repository.fail_summary(summary.id, "LLM timeout")
    body = api.get(f"/api/v1/summaries/{summary.id}").json()
    assert body["messageState"] == "fallida" and body["error"] == "LLM timeout"


def test_get_summary_unknown_id_returns_404(api):
    response = api.get("/api/v1/summaries/unknown")
    assert response.status_code == 404
    assert "detail" in response.json()


# --- GET /api/v1/channels/{channel_id}/summaries ---

def test_list_summaries_returns_every_state_of_the_channel_only(api, repository):
    done = completed(repository)
    pending = repository.create_summary("channel")
    repository.create_summary("other")
    response = api.get(f"{CHANNEL}/summaries")
    assert response.status_code == 200
    assert {item["id"] for item in response.json()} == {done.id, pending.id}


def test_list_summaries_paginates_with_limit_and_offset(api, repository):
    ids = [repository.create_summary("channel").id for _ in range(3)]
    everything = api.get(f"{CHANNEL}/summaries").json()
    assert sorted(item["id"] for item in everything) == sorted(ids)
    assert api.get(f"{CHANNEL}/summaries?limit=1").json() == everything[:1]
    assert api.get(f"{CHANNEL}/summaries?limit=1&offset=2").json() == everything[2:3]


def test_list_summaries_of_unknown_channel_returns_404(api):
    assert api.get("/api/v1/channels/missing/summaries").status_code == 404


@pytest.mark.parametrize("query", ["limit=0", "limit=1001", "offset=-1", "limit=not-an-int"])
def test_list_summaries_with_invalid_pagination_returns_422(api, query):
    assert api.get(f"{CHANNEL}/summaries?{query}").status_code == 422


# --- GET /api/v1/channels/{channel_id}/summaries/latest ---

def test_latest_summary_returns_the_completed_one_ignoring_pending(api, repository):
    done = completed(repository)
    repository.create_summary("channel")
    response = api.get(f"{CHANNEL}/summaries/latest")
    assert response.status_code == 200
    assert response.json()["id"] == done.id


def test_latest_summary_without_completed_ones_returns_404(api, repository):
    repository.create_summary("channel")
    assert api.get(f"{CHANNEL}/summaries/latest").status_code == 404


def test_latest_summary_of_unknown_channel_returns_404(api):
    assert api.get("/api/v1/channels/missing/summaries/latest").status_code == 404


# --- GET /api/v1/events ---

def test_events_lists_the_request_event_of_a_post(api, main_api):
    summary_id = api.post(f"{CHANNEL}/summaries", json={"user_id": "user"}).json()["summary_id"]
    events = main_api.get("/api/v1/events").json()
    assert [(e["type"], e["subject_id"]) for e in events] == [("summary.requested", summary_id)]
    assert events[0]["data"]["channel_id"] == "channel"


def test_events_paginate_with_limit_and_offset(api, main_api):
    for _ in range(3):
        api.post(f"{CHANNEL}/summaries")
    everything = main_api.get("/api/v1/events").json()
    assert len(everything) == 3
    assert main_api.get("/api/v1/events?limit=2").json() == everything[:2]
    assert main_api.get("/api/v1/events?limit=5&offset=2").json() == everything[2:]


@pytest.mark.parametrize("query", ["limit=0", "limit=1001", "offset=-1"])
def test_events_with_invalid_pagination_returns_422(main_api, query):
    assert main_api.get(f"/api/v1/events?{query}").status_code == 422


# --- GET /health ---

def test_health_returns_ok(main_api):
    response = main_api.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_rejects_other_methods_with_405(main_api):
    assert main_api.post("/health").status_code == 405
