"""Contratos públicos de los servicios del chat; se descartan campos internos."""
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models import Identifier, LocalMessage, UTCDateTime


class Pagination(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class UserInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)


class User(UserInput):
    id: Identifier


class ChannelInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class Channel(ChannelInput):
    id: Identifier


class MessageInput(BaseModel):
    author_id: Identifier
    content: str = Field(min_length=1, max_length=4000)


class Message(LocalMessage):
    model_config = {"extra": "ignore"}


class PollInput(BaseModel):
    channel_id: Identifier
    author_id: Identifier
    question: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(min_length=2, max_length=10)
    duration_minutes: int = Field(default=5, ge=1, le=1440)


class PollOption(BaseModel):
    option_id: Identifier
    position: int
    text: str
    vote_count: int


class Poll(BaseModel):
    poll_id: Identifier
    channel_id: Identifier
    author_id: Identifier
    question: str
    options: list[PollOption]
    status: Literal["open", "closed", "pending_announcement", "creation_failed"]
    duration_minutes: int
    closes_at: UTCDateTime
    created_at: UTCDateTime
    closed_at: UTCDateTime | None = None
    total_votes: int
    creation_error: str | None = None


class VoteInput(BaseModel):
    voter_id: Identifier
    option_id: Identifier


class Vote(VoteInput):
    poll_id: Identifier
    cast_at: UTCDateTime


class ClosePollInput(BaseModel):
    author_id: Identifier


class SearchInput(BaseModel):
    content: str | None = Field(default=None, max_length=1024)
    author_id: list[Identifier] = Field(default_factory=list)
    has: list[Literal["link", "mention"]] = Field(default_factory=list)
    min_date: UTCDateTime | None = None
    max_date: UTCDateTime | None = None
    sort_by: Literal["timestamp", "relevance"] = "timestamp"
    sort_order: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=25, ge=1, le=25)
    offset: int = Field(default=0, ge=0, le=9975)

    @model_validator(mode="after")
    def ordered(self):
        if self.min_date and self.max_date and self.min_date > self.max_date:
            raise ValueError("El inicio debe ser anterior al fin")
        return self


class SearchMessage(Message):
    highlights: list[str] = Field(default_factory=list)


class SearchResult(BaseModel):
    total_results: int = 0
    messages: list[SearchMessage] = Field(default_factory=list)
    indexing: bool = False
    retry_after: int = 0


class SearchPending(BaseModel):
    code: Literal[110000]
    retry_after: int = Field(ge=1)


class Preview(BaseModel):
    url: str
    final_url: str
    title: str
    description: str = ""
    image_url: str | None = None


class MediaItem(BaseModel):
    id: Identifier
    channel_id: Identifier
    category: Literal["IMAGE", "DOCUMENT", "LINK"]
    created_at: UTCDateTime
    uploader_id: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    url: str | None = None
    preview: Preview | None = None


class Gallery(BaseModel):
    items: list[MediaItem]
    limit: int
    offset: int


class GalleryInput(BaseModel):
    category: Literal["IMAGE", "DOCUMENT", "LINK"] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class Quota(BaseModel):
    channel_id: Identifier
    used_bytes: int
    available_bytes: int
    limit_bytes: int
    file_count: int
    max_files: int
    max_file_bytes: int
