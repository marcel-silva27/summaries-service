"""Flujo integrado: núcleo real (users, channels, messages) + resúmenes (mock en lugar de Gemini)."""
from conftest import URLS, send_messages, wait_for

SUMMARIES = URLS["summaries"]


def test_summary_of_real_chat_messages_is_generated_end_to_end(http, chat):
    send_messages(http, chat, ["Hola equipo", "Decidimos lanzar el viernes", "Ana, me encargo de la entrega"])
    channel_id = chat["channel"]["id"]

    accepted = http.post(f"{SUMMARIES}/api/v1/channels/{channel_id}/summaries", json={"user_id": chat["user"]["id"]})
    assert accepted.status_code == 202
    assert accepted.json()["messageState"] == "pendiente"

    summary = wait_for(http, f"{SUMMARIES}{accepted.headers['Location']}",
                       lambda body: body["messageState"] in ("completada", "fallida"))
    assert summary["messageState"] == "completada", summary.get("error")
    assert summary["channel_id"] == channel_id
    assert summary["requested_by"] == chat["user"]["id"]
    # El mock cita a los autores: el servicio resolvió el nombre con la API real de users.
    assert chat["user"]["display_name"] in summary["content"]
    assert "3 mensajes" in summary["content"]

    latest = http.get(f"{SUMMARIES}/api/v1/channels/{channel_id}/summaries/latest")
    assert latest.status_code == 200 and latest.json()["id"] == summary["id"]


def test_the_summary_events_are_emitted_for_the_transaction(http, chat):
    send_messages(http, chat, ["uno", "dos", "tres"])
    accepted = http.post(f"{SUMMARIES}/api/v1/channels/{chat['channel']['id']}/summaries")
    summary_id = accepted.json()["summary_id"]
    wait_for(http, f"{SUMMARIES}/api/v1/summaries/{summary_id}", lambda body: body["messageState"] == "completada")

    # `generated` se guarda en la misma escritura que cierra el resumen (outbox).
    types = [event["type"] for event in http.get(f"{SUMMARIES}/api/v1/events", params={"limit": 1000}).json()
             if event["subject_id"] == summary_id]
    assert "summary.requested" in types and "summary.generated" in types


def test_unknown_channel_is_rejected_using_the_real_channels_service(http, chat):
    response = http.post(f"{SUMMARIES}/api/v1/channels/does-not-exist/summaries")
    assert response.status_code == 404


def test_unknown_user_is_rejected_using_the_real_users_service(http, chat):
    response = http.post(f"{SUMMARIES}/api/v1/channels/{chat['channel']['id']}/summaries",
                         json={"user_id": "does-not-exist"})
    assert response.status_code == 422


def test_core_message_events_reach_the_summaries_broker(http, chat):
    """El núcleo publica chat.message.created al broker compartido (demo 03: exchange `chat`, topic)."""
    send_messages(http, chat, ["evento uno"])
    events = http.get(f"{URLS['messages']}/api/v1/events").json()
    assert any(e["type"] == "chat.message.created" and e["data"]["channel_id"] == chat["channel"]["id"]
               for e in events)
