from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_actor, get_service
from app.schemas.chats import ChatMessageCreate, ChatSummary, ReadResult
from app.schemas.matching import Message

# Chat list with unread counters (HU-49). A conversation is an accepted match, so the
# authorization is the same as in /api/v1/matching/matches: only its two participants.
router = APIRouter(prefix="/api/v1/chats", tags=["Chats"])


@router.get("", response_model=list[ChatSummary])
def chats(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
          actor=Depends(get_actor), service=Depends(get_service)):
    return service.chats(actor, limit, offset)


@router.get("/{conversation_id}/messages", response_model=list[Message])
def messages(conversation_id: UUID, after_id: int | None = Query(None, ge=0, le=9223372036854775807),
             before_id: int | None = Query(None, ge=1, le=9223372036854775807), limit: int = Query(50, ge=1, le=100),
             actor=Depends(get_actor), service=Depends(get_service)):
    """Same page as /matching/matches/{id}/messages. Opening it marks the other person's messages as read."""
    return service.messages(actor, conversation_id, after_id, before_id, limit)


@router.post("/{conversation_id}/messages", response_model=Message)
def send(conversation_id: UUID, payload: ChatMessageCreate, actor=Depends(get_actor), service=Depends(get_service)):
    """Send text, an image or file link, or a location. A retry with the same clientMessageId
    returns the same message; the same id with different content is a 409."""
    return service.send_message(actor, conversation_id, payload)


@router.patch("/{conversation_id}/read", response_model=ReadResult)
def read(conversation_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    return service.mark_read(actor, conversation_id)
