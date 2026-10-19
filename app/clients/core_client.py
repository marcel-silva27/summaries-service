"""Cliente síncrono de las APIs públicas de users, channels y messages."""
from math import isfinite
from typing import Annotated, Self
from urllib.parse import quote

import httpx
from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from .. import config
from ..errors import ChannelNotFound, DependencyFailed, DependencyUnavailable, ServiceError, UserNotFound
from ..models import Identifier, LocalMessage


class _Channel(BaseModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=80)


class _User(BaseModel):
    id: Identifier
    display_name: str = Field(min_length=1, max_length=80)


class _Message(LocalMessage):
    # Tolerar campos nuevos del núcleo sin incorporarlos al contrato local.
    model_config = ConfigDict(extra="ignore")

    @field_validator("created_at", mode="before")
    @classmethod
    def iso_timestamp(cls, value):
        if not isinstance(value, str):
            raise ValueError("created_at must be an ISO8601 string")
        return value


_MESSAGES = TypeAdapter(list[_Message])


class CoreClient:
    def __init__(self, *, users_url: str | None = None, channels_url: str | None = None,
                 messages_url: str | None = None, timeout_seconds: float | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.users_url = self._base_url(config.USERS_URL if users_url is None else users_url)
        self.channels_url = self._base_url(config.CHANNELS_URL if channels_url is None else channels_url)
        self.messages_url = self._base_url(config.MESSAGES_URL if messages_url is None else messages_url)
        timeout = config.CORE_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("CORE_TIMEOUT_SECONDS must be positive and finite")
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False,
                                  trust_env=False, headers={"Accept": "application/json"})

    @staticmethod
    def _base_url(value: str) -> str:
        url = httpx.URL(value)
        if url.scheme not in ("http", "https") or not url.host or url.query or url.fragment:
            raise ValueError("Core service URLs must be HTTP(S) base URLs without query or fragment")
        return str(url).rstrip("/")

    @staticmethod
    def _id(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("A non-empty resource ID is required")
        return quote(value, safe="").replace(".", "%2E")

    def _get(self, url: str, not_found: type[ServiceError]):
        try:
            response = self._http.get(url)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProxyError) as exc:
            raise DependencyUnavailable() from exc
        except httpx.RequestError as exc:
            raise DependencyFailed() from exc
        if response.status_code == 404:
            raise not_found()
        if response.status_code in (429, 503, 504):
            raise DependencyUnavailable()
        if response.status_code != 200:
            raise DependencyFailed()
        try:
            return response.json()
        except ValueError as exc:
            raise DependencyFailed("Core service returned invalid JSON") from exc

    def get_channel(self, channel_id: str) -> dict:
        data = self._get(f"{self.channels_url}/api/v1/channels/{self._id(channel_id)}", ChannelNotFound)
        return self._entity(_Channel, data, channel_id)

    def get_user(self, user_id: str) -> dict:
        data = self._get(f"{self.users_url}/api/v1/users/{self._id(user_id)}", UserNotFound)
        return self._entity(_User, data, user_id)

    @staticmethod
    def _entity(model: type[BaseModel], data, expected_id: str) -> dict:
        try:
            entity = model.model_validate(data)
        except ValidationError as exc:
            raise DependencyFailed("Core service returned an invalid resource") from exc
        if entity.id != expected_id:
            raise DependencyFailed("Core service returned a different resource")
        return entity.model_dump(mode="json")

    def list_messages(self, channel_id: str) -> list[dict]:
        data = self._get(f"{self.messages_url}/api/v1/channels/{self._id(channel_id)}/messages", ChannelNotFound)
        try:
            messages = _MESSAGES.validate_python(data)
        except ValidationError as exc:
            raise DependencyFailed("Core service returned invalid messages") from exc
        if any(message.channel_id != channel_id for message in messages):
            raise DependencyFailed("Core service returned messages from a different channel")
        return [message.model_dump(mode="json") for message in messages]

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args) -> None:
        self.close()


def get_core_client(request: Request) -> CoreClient:
    return request.app.state.core_client


CoreClientDependency = Annotated[CoreClient, Depends(get_core_client)]
