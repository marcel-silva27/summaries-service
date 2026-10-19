"""Contratos públicos: esquemas JSON de los eventos y documentación OpenAPI."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate as validate_openapi

from app import config
from app.api_models import ErrorResponse, EventOut
from app.event_schemas import EVENTS, PUBLISHED, json_schema
from app.main import app
from app.models import StructuredSummary, Summary, SummaryAccepted, SummaryRequest
from app.tools.export_event_schemas import render
from test_automatic import enter, messages
from test_worker import Core, msg, seed, worker_for

DOCS = Path(__file__).resolve().parent.parent / "docs" / "eventos"


# --- Esquemas de eventos ---

@pytest.mark.parametrize("event_type", list(EVENTS))
def test_event_schema_files_are_in_sync_with_the_pydantic_models(event_type):
    path = DOCS / f"{event_type}.schema.json"
    assert path.exists(), "Ejecuta: python -m app.tools.export_event_schemas"
    assert path.read_text(encoding="utf-8") == render(event_type), (
        f"{path.name} desactualizado: ejecuta python -m app.tools.export_event_schemas")
    Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("event_type", list(EVENTS))
def test_event_examples_validate_against_both_pydantic_and_the_json_schema_file(event_type):
    examples = json_schema(event_type)["examples"]
    validator = Draft202012Validator(json.loads((DOCS / f"{event_type}.schema.json").read_text(encoding="utf-8")))
    for example in examples:
        EVENTS[event_type].model_validate(example)
        assert not list(validator.iter_errors(example))


def test_every_event_the_service_really_emits_conforms_to_its_schema(repository, monkeypatch):
    monkeypatch.setattr(config, "MESSAGE_THRESHOLD", 3)
    core = Core()
    seed(repository, core, messages(2))
    worker = worker_for(repository, core)
    manual = worker.service.request_summary("channel", SummaryRequest(user_id="u1"))
    repository.receive_summary_request(repository.summaries.find_one({"id": manual.id})["_request_delivery"]["event"])
    worker.drain()  # generated
    seed(repository, core, [msg("bad", "[mock:fail]", at=messages(1, start=9)[0].created_at)])
    failing = worker.service.request_summary("channel")
    repository.receive_summary_request(repository.summaries.find_one({"id": failing.id})["_request_delivery"]["event"])
    worker.drain()  # failed
    seed(repository, core, messages(4, start=100))
    enter(repository, "e1")
    worker.drain()  # solicitud automática
    emitted = repository.list_events()
    assert {e["type"] for e in emitted} == set(PUBLISHED)
    assert any(e["data"].get("trigger") == "automatic" for e in emitted)
    assert any(e["data"].get("trigger") is None for e in emitted if e["type"] == "summary.requested")
    for event in emitted:
        EVENTS[event["type"]].model_validate(event)
        assert not list(Draft202012Validator(json_schema(event["type"])).iter_errors(event))


def test_schemas_reject_malformed_events():
    base = json_schema("summary.generated")["examples"][0]
    bad = [base | {"type": "summary.failed"}, base | {"data": base["data"] | {"messageState": "fallida"}},
           base | {"data": base["data"] | {"content": "no viaja"}}, base | {"extra": 1}]
    for event in bad:
        with pytest.raises(ValueError):
            EVENTS["summary.generated"].model_validate(event)
        assert list(Draft202012Validator(json_schema("summary.generated")).iter_errors(event))
    with pytest.raises(ValueError):  # Los formatos (fecha) los valida Pydantic; JSON Schema no los exige por defecto.
        EVENTS["summary.generated"].model_validate(base | {"occurred_at": "ayer"})


# --- OpenAPI ---

OPENAPI = app.openapi()
OPERATIONS = [(path, method, op) for path, item in OPENAPI["paths"].items() for method, op in item.items()]


def test_the_openapi_document_is_valid():
    validate_openapi(OPENAPI)
    assert OPENAPI["info"]["version"] == "1.0.0" and "Flujo" in OPENAPI["info"]["description"]
    assert {tag["name"] for tag in OPENAPI["tags"]} >= {"summaries", "events", "health"}


@pytest.mark.parametrize("path,method,operation", OPERATIONS)
def test_every_operation_is_documented(path, method, operation):
    assert operation["operationId"] and operation["summary"] and operation["description"] and operation["tags"]
    for name, response in operation["responses"].items():
        assert response["description"], f"{method} {path} {name} sin descripción"
    for parameter in operation.get("parameters", []):
        assert parameter.get("description"), f"{method} {path}: parámetro {parameter['name']} sin descripción"


def test_operation_ids_are_unique():
    ids = [op["operationId"] for _, _, op in OPERATIONS]
    assert len(ids) == len(set(ids))


def test_errors_are_documented_with_the_uniform_error_schema():
    expected = {("/api/v1/channels/{channel_id}/summaries", "post"): {"202", "404", "422", "502", "503"},
                ("/api/v1/channels/{channel_id}/summaries", "get"): {"200", "404", "422", "502", "503"},
                ("/api/v1/channels/{channel_id}/summaries/latest", "get"): {"200", "404", "502", "503"},
                ("/api/v1/summaries/{summary_id}", "get"): {"200", "404", "503"},
                ("/api/v1/events", "get"): {"200", "422", "503"}}
    for (path, method), codes in expected.items():
        responses = OPENAPI["paths"][path][method]["responses"]
        assert codes <= set(responses), (path, method, set(responses))
        for code in codes - {"200", "202"}:
            schema = responses[code]["content"]["application/json"]["schema"]
            assert schema["$ref"].endswith("/ErrorResponse"), (path, method, code)
    accepted = OPENAPI["paths"]["/api/v1/channels/{channel_id}/summaries"]["post"]["responses"]["202"]
    assert "Location" in accepted["headers"]


def test_schemas_carry_examples_that_are_valid_instances():
    schemas = OPENAPI["components"]["schemas"]
    for name, model in (("Summary", Summary), ("SummaryAccepted", SummaryAccepted), ("SummaryRequest", SummaryRequest)):
        assert schemas[name]["examples"], name
        for example in schemas[name]["examples"]:
            model.model_validate(example)
    assert "structured" in schemas["Summary"]["properties"] and "StructuredSummary" in schemas
    assert {"ErrorResponse", "EventOut", "Health"} <= set(schemas)
    ErrorResponse.model_validate({"detail": "Channel not found"})
    ErrorResponse.model_validate({"detail": [{"loc": ["query", "limit"], "msg": "x", "type": "y"}]})
    EventOut.model_validate(json_schema("summary.generated")["examples"][0])
    StructuredSummary.model_validate(schemas["Summary"]["examples"][0]["structured"])
