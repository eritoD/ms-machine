"""Chat WebSocket (HU-52): `wss://<gateway>/api/v1/chats/ws`.

Protocol (JSON text frames, at most 4 KB):

  client -> server
    {"type": "auth", "token": "<access token>"}        first frame, within 5 s
    {"type": "join", "conversationId": "<uuid>"}       the chat is open on screen (one per socket)
    {"type": "leave"}                                  the chat was closed
    {"type": "ping"}                                   answered with {"type": "pong"}

  server -> client
    {"type": "ready", "userId": "<uuid>"}
    {"type": "joined" | "left", "conversationId": "<uuid>"}
    {"type": "new_message", "conversationId": "<uuid>", "message": {...same as the REST message...}}
        to every socket of the two participants that has that conversation joined
    {"type": "unread_update", "conversationId": "<uuid>", "unreadCount": 3, "totalUnread": 7,
     "lastMessage": {"content": "...", "senderId": "<uuid>", "createdAt": "...", "type": "TEXT"}}
        to the recipient's sockets that do NOT have that conversation joined (badge and list)
    {"type": "error", "code": "...", "message": "..."}

The token goes in the first frame, not in the URL, so it never lands in access logs. The socket
closes when the token expires (4401: reconnect with a fresh one); other close codes: 4409 replaced
by a newer connection, 1008 too many frames, 1009 frame too large, 1013 service unavailable.
"""
import asyncio
import json
import time
from collections import deque
from uuid import UUID

import anyio
import jwt
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.core.security import authenticate
from app.services.realtime import Connection

router = APIRouter()

AUTH_TIMEOUT_SECONDS = 5
MAX_FRAME_CHARACTERS = 4096
MAX_FRAMES, WINDOW_SECONDS = 30, 10


def error(code: str, message: str, **extra):
    return {"type": "error", "code": code, "message": message, **extra}


async def _authenticate(websocket: WebSocket, service, settings):
    """The actor and the token's expiry, or None after closing the socket."""
    code = 4401
    try:
        text = await asyncio.wait_for(websocket.receive_text(), AUTH_TIMEOUT_SECONDS)
        frame = json.loads(text) if len(text) <= MAX_FRAME_CHARACTERS else None
        if not isinstance(frame, dict) or frame.get("type") != "auth" or not isinstance(frame.get("token"), str):
            raise ValueError("The first frame must be an auth frame")
        token = frame["token"]
        actor = authenticate(token, settings)
        # Users checks the account is still active, exactly as on every REST call.
        await anyio.to_thread.run_sync(service.users.card, actor.id, token)
        return actor, float(jwt.decode(token, options={"verify_signature": False})["exp"])
    except HTTPException as failure:
        code = 1013 if failure.status_code >= 500 else 4401
    except WebSocketDisconnect:
        return None
    except (asyncio.TimeoutError, ValueError, KeyError, TypeError, jwt.PyJWTError):
        pass
    try:
        await websocket.close(code)
    except RuntimeError:
        pass
    return None


async def _writer(connection: Connection):
    """The only coroutine that writes to the socket, so frames keep the order they were queued in."""
    while True:
        event = await connection.queue.get()
        try:
            await connection.websocket.send_json(event)
        except Exception:
            connection.closed = True
            return
        finally:
            connection.queue.task_done()


async def _handle(frame, connection: Connection, actor, service, hub):
    kind = frame.get("type") if isinstance(frame, dict) else None
    if kind == "ping":
        hub.enqueue(connection, {"type": "pong"})
    elif kind in ("join", "leave"):
        if kind == "leave":
            connection.joined = None
            hub.enqueue(connection, {"type": "left"})
            return
        try:
            conversation_id = UUID(str(frame.get("conversationId")))
        except ValueError:
            return hub.enqueue(connection, error("invalid_conversation", "conversationId debe ser un UUID."))
        try:
            # The same checks as opening the chat over REST: a participant of an accepted match, and
            # no block between the two people.
            await anyio.to_thread.run_sync(service.match, actor, conversation_id)
        except HTTPException as failure:
            code = "unavailable" if failure.status_code >= 500 else "conversation_unavailable"
            return hub.enqueue(connection, error(code, "No puedes abrir esta conversación.",
                                                 conversationId=str(conversation_id)))
        connection.joined = conversation_id
        hub.enqueue(connection, {"type": "joined", "conversationId": str(conversation_id)})
    elif kind == "auth":
        hub.enqueue(connection, error("already_authenticated", "La conexión ya está autenticada."))
    else:
        hub.enqueue(connection, error("unknown_type", "Tipo de mensaje desconocido."))


@router.websocket("/api/v1/chats/ws")
async def chat_socket(websocket: WebSocket):
    await websocket.accept()
    state = websocket.app.state
    hub, service = state.hub, state.service
    authenticated = await _authenticate(websocket, service, state.settings)
    if authenticated is None:
        return
    actor, expires_at = authenticated
    connection = Connection(websocket, actor.id)
    hub.close_evicted(hub.register(connection))
    writer = asyncio.create_task(_writer(connection))
    hub.enqueue(connection, {"type": "ready", "userId": str(actor.id)})
    close_code, recent = 1000, deque()
    try:
        while not connection.closed:
            remaining = expires_at - time.time()
            if remaining <= 0:
                hub.enqueue(connection, error("token_expired", "La sesión venció: vuelve a conectar con un token nuevo."))
                close_code = 4401
                break
            try:
                text = await asyncio.wait_for(websocket.receive_text(), remaining)
            except asyncio.TimeoutError:
                continue  # the token expired meanwhile; the loop sends the error and closes
            now = time.monotonic()
            recent.append(now)
            while recent and now - recent[0] > WINDOW_SECONDS:
                recent.popleft()
            if len(recent) > MAX_FRAMES:
                close_code = 1008
                break
            if len(text) > MAX_FRAME_CHARACTERS:
                close_code = 1009
                break
            try:
                frame = json.loads(text)
            except ValueError:
                hub.enqueue(connection, error("invalid_frame", "El mensaje debe ser JSON."))
                continue
            await _handle(frame, connection, actor, service, hub)
    except WebSocketDisconnect:
        close_code = None
    finally:
        hub.unregister(connection)
        if close_code is not None and not connection.closed:
            try:  # let the last queued frames (an error, for instance) go out before closing
                await asyncio.wait_for(connection.queue.join(), 2)
            except asyncio.TimeoutError:
                pass
        connection.closed = True
        writer.cancel()
        if close_code is not None:
            try:
                await websocket.close(close_code)
            except RuntimeError:
                pass
