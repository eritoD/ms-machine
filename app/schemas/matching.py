from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient_id: UUID


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["accept", "reject"]


MAX_MESSAGE_ID = 9223372036854775807


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_message_id: UUID
    text: str = Field(min_length=1, max_length=2000)
    # HU-64: the message of the same chat this one answers.
    reply_to: int | None = Field(default=None, ge=1, le=MAX_MESSAGE_ID)


class Sport(BaseModel):
    deporte_codigo: str
    nivel: int


class AthleteCard(BaseModel):
    user_id: UUID
    nombre: str
    apellido_inicial: str
    edad: int | None
    foto_perfil: str | None
    biografia: str | None
    deportes: list[Sport]
    compatibilidad: int


class Connection(BaseModel):
    id: UUID
    sender_id: UUID
    recipient_id: UUID
    status: Literal["pending", "accepted", "rejected"]
    created_at: datetime
    decided_at: datetime | None
    athlete: AthleteCard
    last_message: str | None = None
    last_message_at: datetime | None = None


class MatchingState(BaseModel):
    incoming: list[Connection]
    outgoing: list[Connection]
    matches: list[Connection]
    hidden_user_ids: list[UUID]


class ReplyPreview(BaseModel):
    """HU-64: what a reply shows of the message it quotes. Never the whole message: the text is cut short, and a
    quoted message that is gone says so (`available` false, every other field null) without showing what it said."""

    id: str | None
    sender_id: UUID | None
    text: str | None
    message_type: Literal["TEXT", "IMAGE", "LOCATION", "FILE"] | None
    available: bool


class Message(BaseModel):
    id: str
    match_id: UUID
    sender_id: UUID
    client_message_id: UUID
    text: str
    created_at: datetime
    # Set when the recipient has read it. For a message you sent, that is the read receipt.
    read_at: datetime | None = None
    is_read: bool = False
    # What the message carries (HU-50). For IMAGE and LOCATION `text` is an optional caption.
    message_type: Literal["TEXT", "IMAGE", "LOCATION", "FILE"] = "TEXT"
    attachment_url: str | None = None
    attachment_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_name: str | None = None
    # HU-64: set when this message answers another one of the same chat.
    reply_to_id: str | None = None
    reply_to: ReplyPreview | None = None
