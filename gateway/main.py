"""Gateway del chat: GraphQL para datos, HTTP para archivos y web del mismo origen."""
import os
from contextlib import asynccontextmanager
from math import isfinite
from pathlib import Path
from string import Formatter
from typing import Annotated
from urllib.parse import quote

import httpx
from ariadne import MutationType, QueryType, format_error, load_schema_from_path, make_executable_schema
from ariadne.asgi import GraphQL
from fastapi import FastAPI, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from graphql import GraphQLError
from pydantic import TypeAdapter, ValidationError

from app.api_models import Health
from app.models import Summary, SummaryAccepted, SummaryRequest
from .models import (
    Channel, ChannelInput, ClosePollInput, Gallery, GalleryInput, MediaItem, Message, MessageInput,
    Pagination, Poll, PollInput, Quota, SearchInput, SearchPending, SearchResult, User, UserInput, Vote, VoteInput,
)


def failure(status: int, detail: str, service="summaries") -> GraphQLError:
    return GraphQLError(detail, extensions={"detail": detail, "status": status, "service": service})


def segment(value: str) -> str:
    return quote(value, safe="").replace(".", "%2E")


async def upstream(state, service, path, **kwargs):
    try:
        response = await state.http.request(url=state.services[service] + "/api/v1" + path, **kwargs)
    except httpx.TimeoutException:
        raise failure(504, "Tiempo de espera agotado", service) from None
    except httpx.RequestError:
        raise failure(503, "Servicio no disponible", service) from None
    if not response.is_success:
        status = response.status_code if 400 <= response.status_code < 500 else 502
        raise failure(status, "El servicio rechazó la solicitud", service)
    return response


def public_data(response, output, service):
    try:
        adapter = TypeAdapter(output)
        return adapter.dump_python(adapter.validate_python(response.json()), mode="json")
    except ValueError:
        raise failure(502, "Respuesta inválida del servicio", service) from None


async def fetch(info, path, output, *, service="summaries", method="GET", **kwargs):
    response = await upstream(info.context["request"].app.state, service, path, method=method, **kwargs)
    if service == "search" and response.status_code == 202:
        pending = public_data(response, SearchPending, service)
        return SearchResult(indexing=True, retry_after=pending["retry_after"]).model_dump()
    return public_data(response, output, service)


query, mutation = QueryType(), MutationType()


def bind(target, field, path, output, *, service="summaries", input_model=None,
         paginated=False, method=None, identity=False):
    """Validación y transporte comunes; cada operación conserva el contrato de su dueño."""
    path_keys = [name for _, name, _, _ in Formatter().parse(path) if name]
    method = method or ("POST" if input_model else "GET")

    @target.field(field)
    async def resolve(_, info, **args):
        segments = {key: segment(args.pop(key)) for key in path_keys}
        headers = {}
        if identity:
            headers["X-User-Id"] = args.pop("user_id")
        if key := args.pop("idempotency_key", None):
            headers["Idempotency-Key"] = key
        try:
            body = None
            if input_model:
                values = input_model.model_validate(args.pop("input", None) or {})
                if method == "GET":
                    args.update(values.model_dump(mode="json", exclude_none=True))
                else:
                    body = values.model_dump(mode="json")
            params = Pagination(**args).model_dump() if paginated else args
        except ValidationError:
            raise failure(422, "Datos de entrada inválidos", service) from None
        return await fetch(info, path.format(**segments), output, service=service,
                           method=method, params=params, json=body, headers=headers)


bind(query, "getSummary", "/summaries/{id}", Summary)
bind(query, "listSummaries", "/channels/{channel_id}/summaries", list[Summary], paginated=True)
bind(query, "getLatestSummary", "/channels/{channel_id}/summaries/latest", Summary)
bind(mutation, "requestSummary", "/channels/{channel_id}/summaries", SummaryAccepted, input_model=SummaryRequest)
bind(query, "listUsers", "/users", list[User], service="users")
bind(query, "getUser", "/users/{id}", User, service="users")
bind(query, "listChannels", "/channels", list[Channel], service="channels")
bind(query, "listMessages", "/channels/{channel_id}/messages", list[Message], service="messages")
bind(mutation, "createUser", "/users", User, service="users", input_model=UserInput)
bind(mutation, "createChannel", "/channels", Channel, service="channels", input_model=ChannelInput)
bind(mutation, "sendMessage", "/channels/{channel_id}/messages", Message, service="messages", input_model=MessageInput)
bind(query, "searchMessages", "/channels/{channel_id}/messages/search", SearchResult,
     service="search", input_model=SearchInput, method="GET")
bind(query, "getPoll", "/polls/{id}", Poll, service="polls")
bind(mutation, "createPoll", "/polls", Poll, service="polls", input_model=PollInput)
bind(mutation, "votePoll", "/polls/{id}/votes", Vote, service="polls", input_model=VoteInput)
bind(mutation, "closePoll", "/polls/{id}/close", Poll, service="polls", input_model=ClosePollInput)
bind(query, "listAttachments", "/channels/{channel_id}/attachments", Gallery,
     service="media", input_model=GalleryInput, method="GET", identity=True)
bind(query, "getQuota", "/channels/{channel_id}/quota", Quota, service="media", identity=True)

schema = make_executable_schema(load_schema_from_path(str(Path(__file__).with_name("schema.graphql"))), query, mutation)


def public_error(error, debug=False):
    if error.original_error and not isinstance(error.original_error, GraphQLError):
        return {"message": "Error interno", "extensions": {"detail": "Error interno", "status": 500}}
    result = format_error(error, debug=False)
    result.setdefault("extensions", {"detail": "Consulta GraphQL inválida", "status": 400})
    return result


def create_app(*, transport=None) -> FastAPI:
    services = {name: os.environ.get(f"{name.upper()}_URL", default).rstrip("/") for name, default in {
        "users": "http://users:8000", "channels": "http://channels:8000", "messages": "http://messages:8000",
        "summaries": "http://summaries:8000", "media": "http://media:8000",
        "search": "http://search:8000", "polls": "http://polls-api:8004",
    }.items()}
    for url in services.values():
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host or parsed.query or parsed.fragment or parsed.userinfo:
            raise ValueError("Las URLs deben ser HTTP(S), sin credenciales, query ni fragmento")
    timeout = float(os.environ.get("GATEWAY_TIMEOUT_SECONDS", "10"))
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("GATEWAY_TIMEOUT_SECONDS debe ser positivo y finito")

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=timeout, transport=transport, trust_env=False,
                                     follow_redirects=False) as client:
            app.state.http, app.state.services = client, services
            yield

    app = FastAPI(title="Chat API Gateway", version="1.0.0", lifespan=lifespan)
    origins = os.environ.get("GATEWAY_CORS_ORIGINS", "http://localhost:3000,http://localhost:5173")
    app.add_middleware(CORSMiddleware, allow_origins=[value.strip() for value in origins.split(",") if value.strip()],
                       allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                       allow_headers=["Content-Type", "Authorization", "X-User-Id"])

    @app.exception_handler(GraphQLError)
    async def http_error(_, error):
        return JSONResponse({"detail": error.message}, status_code=error.extensions["status"])

    @app.get("/health", response_model=Health)
    def health():
        return Health()

    graphql = GraphQL(schema, debug=False, error_formatter=public_error)

    @app.api_route("/api/v1/graphql", methods=["GET", "POST"], include_in_schema=False)
    async def graphql_endpoint(request: Request):
        return await graphql.handle_request(request)

    identity = Annotated[str, Header(min_length=1)]

    @app.post("/api/v1/media/channels/{channel_id}/attachments", response_model=MediaItem, status_code=201)
    async def upload(channel_id: str, file: UploadFile, x_user_id: identity):
        try:
            content = await file.read(10 * 1024 * 1024 + 1)
        finally:
            await file.close()
        if not content or len(content) > 10 * 1024 * 1024:
            raise failure(413 if content else 422, "El archivo debe contener entre 1 byte y 10 MiB", "media")
        response = await upstream(app.state, "media", f"/channels/{segment(channel_id)}/attachments", method="POST",
                                  files={"file": (file.filename, content, file.content_type)},
                                  headers={"X-User-Id": x_user_id})
        return public_data(response, MediaItem, "media")

    @app.get("/api/v1/media/attachments/{attachment_id}/download")
    async def download(attachment_id: str, x_user_id: identity):
        response = await upstream(app.state, "media", f"/attachments/{segment(attachment_id)}/download",
                                  method="GET", headers={"X-User-Id": x_user_id})
        headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
        if disposition := response.headers.get("content-disposition"):
            headers["Content-Disposition"] = disposition
        return Response(response.content, media_type=response.headers.get("content-type", "application/octet-stream"),
                        headers=headers)

    @app.delete("/api/v1/media/attachments/{attachment_id}", status_code=204)
    async def delete_attachment(attachment_id: str, x_user_id: identity):
        await upstream(app.state, "media", f"/attachments/{segment(attachment_id)}", method="DELETE",
                       headers={"X-User-Id": x_user_id})
        return Response(status_code=204)

    app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent.parent / "web", html=True), name="web")
    return app


app = create_app()
