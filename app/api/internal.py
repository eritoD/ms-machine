"""Endpoint interno de Ms_Matching (HU-47): quién escribió un mensaje del chat, para que Ms_Users pueda sancionar a la
persona autora de un mensaje reportado.

Nunca se declara en el gateway ni se publica fuera de Docker: otros servicios lo llaman por la red interna con el
secreto compartido INTERNAL_SERVICE_TOKEN en el encabezado X-Internal-Service-Token. Un token de usuario no sirve."""
from __future__ import annotations

import hmac
import os
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status

MIN_TOKEN_BYTES = 32
router = APIRouter(prefix="/internal/v1/messages", tags=["internal"], include_in_schema=False)


def _expected_token() -> str | None:
    token = os.environ.get("INTERNAL_SERVICE_TOKEN")
    return token if token and len(token.encode("utf-8")) >= MIN_TOKEN_BYTES else None


def require_internal_service(token: str | None) -> None:
    expected = _expected_token()
    if expected is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "internal authentication is not configured")
    if token is None or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "service authentication required")


@router.get("/{match_id}/{message_id}/author")
def message_author(
    match_id: UUID,
    message_id: int,
    request: Request,
    x_internal_service_token: str | None = Header(default=None, max_length=256),
) -> dict[str, str]:
    """Only the account that wrote it: never the text of the message or the other person of the chat."""
    require_internal_service(x_internal_service_token)
    if message_id < 1 or message_id > 9_223_372_036_854_775_807:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    sender = request.app.state.service.repository.message_author(match_id, message_id)
    if sender is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    return {"user_id": str(sender)}
