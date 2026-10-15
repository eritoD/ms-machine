"""HU-52: real-time chat events over WebSockets. Real PostgreSQL in an isolated schema."""
import asyncio
import faulthandler
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.v1.realtime as realtime_api
from app.main import create_app
from app.services.matching_service import MatchingService
from app.services.realtime import MAX_CONNECTIONS_PER_USER, RealtimeHub
from test_matching import A, B, C, SECRET, decide, headers, request, system, token  # noqa: F401 (fixture)

WS = "/api/v1/chats/ws"
CHATS = "/api/v1/chats"
HOSTS = ("files.sportmatch.test",)


@pytest.fixture(autouse=True)
def never_hang():
    """A broken test must fail, not freeze the whole run waiting for a frame that never comes."""
    faulthandler.dump_traceback_later(60, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


@pytest.fixture
def live(system):
    _, repository, directory, settings = system
    hub = RealtimeHub()
    service = MatchingService(repository, directory, attachment_hosts=HOSTS, realtime=hub)
    with TestClient(create_app(service, settings)) as client:
        yield client, hub, directory


@contextmanager
def socket(client, who, join=None):
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "auth", "token": token(who)})
        assert ws.receive_json() == {"type": "ready", "userId": str(who)}
        if join:
            ws.send_json({"type": "join", "conversationId": join})
            assert ws.receive_json() == {"type": "joined", "conversationId": join}
        yield ws


def quiet(ws):
    """Nothing else was queued for this socket: the next frame is the answer to our ping."""
    ws.send_json({"type": "ping"})
    assert ws.receive_json() == {"type": "pong"}


def match(client, sender=A, recipient=B):
    rid = request(client, sender, recipient)["id"]
    assert decide(client, rid, actor=recipient).status_code == 200
    return rid


def post(client, rid, actor=A, **body):
    response = client.post(f"{CHATS}/{rid}/messages", headers=headers(actor), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def closed_with(ws):
    with pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_json()
    return closed.value.code


# -- connecting -------------------------------------------------------------------------------------


def test_the_first_frame_authenticates_and_the_socket_is_ready(live):
    client, hub, _ = live
    with socket(client, A):
        assert hub.is_connected(A) and not hub.is_connected(B)
    deadline = time.time() + 3
    while hub.is_connected(A) and time.time() < deadline:
        time.sleep(0.05)
    assert not hub.is_connected(A)  # closing the socket frees its place


def token_expiring_in(user, seconds):
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user), "iat": now, "exp": now + timedelta(seconds=seconds), "iss": "sportmatch-auth",
                       "aud": "sportmatch-mobile", "token_type": "access"}, SECRET, algorithm="HS256")


@pytest.mark.parametrize(
    "first",
    [
        {"type": "auth", "token": "no-es-un-jwt"},
        {"type": "auth"},
        {"type": "auth", "token": 123},
        {"type": "join", "conversationId": str(uuid4())},
        {"type": "ping"},
        ["auth"],
        "texto",
    ],
)
def test_a_bad_first_frame_or_token_closes_with_4401(live, first):
    client, *_ = live
    with client.websocket_connect(WS) as ws:
        ws.send_json(first)
        assert closed_with(ws) == 4401


def test_an_expired_or_foreign_token_is_refused(live):
    client, *_ = live
    for bad in (token_expiring_in(A, -5), jwt.encode({"sub": str(A)}, SECRET, algorithm="HS256"),
                jwt.encode({"sub": str(A), "exp": int(time.time()) + 60}, "otra-clave-de-mas-de-32-bytes-0123456789", algorithm="HS256")):
        with client.websocket_connect(WS) as ws:
            ws.send_json({"type": "auth", "token": bad})
            assert closed_with(ws) == 4401


def test_a_deactivated_account_cannot_connect(live):
    client, _, directory = live
    directory.active.discard(A)
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "auth", "token": token(A)})
        assert closed_with(ws) == 4401


def test_not_authenticating_in_time_closes_the_socket(live, monkeypatch):
    client, *_ = live
    monkeypatch.setattr(realtime_api, "AUTH_TIMEOUT_SECONDS", 0.3)
    with client.websocket_connect(WS) as ws:
        assert closed_with(ws) == 4401


def test_an_oversized_first_frame_is_refused(live):
    client, *_ = live
    with client.websocket_connect(WS) as ws:
        ws.send_text('{"type": "auth", "token": "' + "x" * 5000 + '"}')
        assert closed_with(ws) == 4401


def test_the_socket_closes_when_the_token_expires(live):
    client, *_ = live
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "auth", "token": token_expiring_in(A, 1.2)})
        assert ws.receive_json()["type"] == "ready"
        frame = ws.receive_json()  # arrives by itself once the token is no longer valid
        assert frame["type"] == "error" and frame["code"] == "token_expired"
        assert closed_with(ws) == 4401


def test_a_sixth_device_replaces_the_oldest_one(live):
    client, hub, _ = live
    with ExitStack() as stack:
        sockets = [stack.enter_context(client.websocket_connect(WS)) for _ in range(MAX_CONNECTIONS_PER_USER)]
        for ws in sockets:
            ws.send_json({"type": "auth", "token": token(A)})
            assert ws.receive_json()["type"] == "ready"
        newest = stack.enter_context(client.websocket_connect(WS))
        newest.send_json({"type": "auth", "token": token(A)})
        assert newest.receive_json()["type"] == "ready"
        assert closed_with(sockets[0]) == 4409
        quiet(sockets[1])  # the others are untouched
        quiet(newest)


# -- the protocol ----------------------------------------------------------------------------------------


def test_ping_and_malformed_frames_do_not_end_the_connection(live):
    client, *_ = live
    with socket(client, A) as ws:
        quiet(ws)
        ws.send_text("{no es json")
        assert ws.receive_json()["code"] == "invalid_frame"
        ws.send_json({"type": "dormir"})
        assert ws.receive_json()["code"] == "unknown_type"
        ws.send_json([1, 2])
        assert ws.receive_json()["code"] == "unknown_type"
        ws.send_json({"type": "auth", "token": token(A)})
        assert ws.receive_json()["code"] == "already_authenticated"
        quiet(ws)


def test_oversized_and_flooding_clients_are_cut_off(live):
    client, *_ = live
    with socket(client, A) as ws:
        ws.send_text('{"type": "ping", "pad": "' + "x" * 5000 + '"}')
        assert closed_with(ws) == 1009
    with socket(client, B) as ws:
        for _ in range(realtime_api.MAX_FRAMES + 5):
            ws.send_json({"type": "ping"})
        code = None
        try:
            while True:
                ws.receive_json()
        except WebSocketDisconnect as closed:
            code = closed.code
        assert code == 1008


def test_only_a_participant_can_open_a_conversation(live):
    client, _, directory = live
    rid = match(client)
    pending = request(client, B, C)["id"]
    with socket(client, C) as ws:
        for conversation in (rid, str(uuid4()), pending):
            ws.send_json({"type": "join", "conversationId": conversation})
            frame = ws.receive_json()
            assert frame["code"] == "conversation_unavailable" and frame["conversationId"] == conversation
        ws.send_json({"type": "join", "conversationId": "no-es-uuid"})
        assert ws.receive_json()["code"] == "invalid_conversation"
        ws.send_json({"type": "join"})
        assert ws.receive_json()["code"] == "invalid_conversation"
    with socket(client, B) as ws:  # B cannot open the pending one: B asked, C has not accepted
        ws.send_json({"type": "join", "conversationId": pending})
        assert ws.receive_json()["code"] == "conversation_unavailable"
        directory.active.discard(A)  # the peer is no longer reachable (deactivated or blocked)
        ws.send_json({"type": "join", "conversationId": rid})
        assert ws.receive_json()["code"] == "conversation_unavailable"


# -- events ----------------------------------------------------------------------------------------------------


def test_new_message_reaches_everyone_who_has_the_conversation_open(live):
    client, hub, _ = live
    rid = match(client)
    with socket(client, A, join=rid) as sender, socket(client, B, join=rid) as recipient:
        assert hub.is_active(B, rid) and hub.is_active(A, rid)
        saved = post(client, rid, actor=A, content="Nos vemos en la cancha")
        for ws in (sender, recipient):  # the sender's own devices stay in sync too
            frame = ws.receive_json()
            assert frame == {"type": "new_message", "conversationId": rid, "message": saved}
        assert saved["text"] == "Nos vemos en la cancha" and saved["sender_id"] == str(A)
        quiet(recipient)  # joined people get new_message only, never unread_update
        quiet(sender)


def test_a_recipient_who_is_elsewhere_gets_unread_update_with_the_counts(live):
    client, hub, _ = live
    with_b = match(client, A, B)
    with_c = match(client, C, B)
    with socket(client, B) as recipient, socket(client, A, join=with_b) as sender:
        assert not hub.is_active(B, with_b)
        post(client, with_b, actor=A, content="Primero")
        first = recipient.receive_json()
        assert first["type"] == "unread_update" and first["conversationId"] == with_b
        assert (first["unreadCount"], first["totalUnread"]) == (1, 1)
        assert first["lastMessage"]["content"] == "Primero" and first["lastMessage"]["senderId"] == str(A)
        assert first["lastMessage"]["type"] == "TEXT" and first["lastMessage"]["createdAt"].endswith("Z")
        post(client, with_c, actor=C, content="Desde otro chat")
        post(client, with_b, actor=A, content="Segundo")
        counts = [(f["conversationId"], f["unreadCount"], f["totalUnread"]) for f in (recipient.receive_json(), recipient.receive_json())]
        assert counts == [(with_c, 1, 2), (with_b, 2, 3)]  # the badge counts every conversation
        assert sender.receive_json()["type"] == "new_message"  # A had the chat open
        assert sender.receive_json()["type"] == "new_message"
        quiet(sender)  # and the sender never gets an unread_update for their own message


def test_leaving_a_conversation_turns_its_events_into_unread_updates(live):
    client, hub, _ = live
    rid = match(client)
    with socket(client, B, join=rid) as ws:
        post(client, rid, actor=A, content="Con el chat abierto")
        assert ws.receive_json()["type"] == "new_message"
        ws.send_json({"type": "leave"})
        assert ws.receive_json() == {"type": "left"}
        assert not hub.is_active(B, rid)
        post(client, rid, actor=A, content="Con el chat cerrado")
        assert ws.receive_json()["type"] == "unread_update"
        ws.send_json({"type": "join", "conversationId": rid})
        assert ws.receive_json()["type"] == "joined"
        assert hub.is_active(B, rid)


def test_each_device_gets_what_fits_it_and_joining_another_chat_moves_the_socket(live):
    client, hub, _ = live
    first = match(client, A, B)
    second = match(client, C, B)
    with socket(client, B, join=first) as phone, socket(client, B) as tablet, socket(client, C, join=second):
        post(client, first, actor=A, content="Para los dos aparatos")
        assert phone.receive_json()["type"] == "new_message"
        assert tablet.receive_json()["type"] == "unread_update"
        phone.send_json({"type": "join", "conversationId": second})  # one open chat per socket
        assert phone.receive_json()["type"] == "joined"
        assert hub.is_active(B, second) and not hub.is_active(B, first)
        post(client, first, actor=A, content="Ya no la miro")
        assert phone.receive_json()["type"] == "unread_update"


def test_events_stay_inside_their_conversation(live):
    client, *_ = live
    rid = match(client, A, B)
    other = match(client, C, B)
    with socket(client, B, join=other) as elsewhere, socket(client, C, join=other) as stranger:
        post(client, rid, actor=A, content="No es para esa conversación")
        assert elsewhere.receive_json()["type"] == "unread_update"  # B is told, but only as a counter
        quiet(stranger)  # C is not in that conversation at all


def test_every_kind_of_message_is_announced(live):
    client, *_ = live
    rid = match(client)
    with socket(client, B) as ws, socket(client, A, join=rid) as sender:
        location = post(client, rid, type="LOCATION", latitude=-33.5, longitude=-70.6)
        frame = ws.receive_json()
        assert frame["lastMessage"] == {**frame["lastMessage"], "content": "Ubicación", "type": "LOCATION"}
        image = post(client, rid, type="IMAGE", attachmentUrl="https://files.sportmatch.test/c.png", content="La cancha")
        assert ws.receive_json()["lastMessage"]["content"] == "La cancha"
        file = post(client, rid, type="FILE", attachmentUrl="https://files.sportmatch.test/r.pdf")
        assert ws.receive_json()["lastMessage"] == {**frame["lastMessage"], "content": "Archivo", "type": "FILE",
                                                    "createdAt": ws_time(file)}
        events = [sender.receive_json() for _ in range(3)]
        assert [e["message"]["id"] for e in events] == [location["id"], image["id"], file["id"]]
        assert events[0]["message"]["latitude"] == -33.5 and events[1]["message"]["attachment_url"].endswith("c.png")


def ws_time(message):
    return message["created_at"].replace("+00:00", "Z") if message["created_at"].endswith("+00:00") else \
        datetime.fromisoformat(message["created_at"]).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_a_retry_of_a_saved_message_is_not_announced_twice(live):
    client, *_ = live
    rid = match(client)
    cid = str(uuid4())
    with socket(client, B, join=rid) as ws:
        first = post(client, rid, actor=A, content="Una sola vez", client_message_id=cid)
        assert ws.receive_json()["message"]["id"] == first["id"]
        again = post(client, rid, actor=A, content="Una sola vez", client_message_id=cid)
        assert again["id"] == first["id"]
        quiet(ws)  # nothing new arrived


def test_rejected_messages_are_not_announced(live):
    client, *_ = live
    rid = match(client)
    with socket(client, B, join=rid) as ws:
        assert client.post(f"{CHATS}/{rid}/messages", headers=headers(A), json={"content": "   "}).status_code == 422
        assert client.post(f"{CHATS}/{rid}/messages", headers=headers(C), json={"content": "intruso"}).status_code == 404
        quiet(ws)


def test_a_failure_in_real_time_never_stops_a_message_from_being_saved(live, monkeypatch):
    client, hub, _ = live
    rid = match(client)

    def broken(*args, **kwargs):
        raise RuntimeError("the socket layer exploded")

    monkeypatch.setattr(hub, "emit_to_conversation", broken)
    monkeypatch.setattr(hub, "is_connected", broken)
    with socket(client, B):
        saved = post(client, rid, actor=A, content="Esto se guarda igual")
    stored = client.get(f"{CHATS}/{rid}/messages", headers=headers(B)).json()
    assert [m["id"] for m in stored] == [saved["id"]]


def test_a_flooded_socket_is_dropped_without_slowing_the_sender(live):
    client, hub, _ = live
    rid = match(client)
    with socket(client, B) as ws:
        connection = hub._by_user[B][0]  # noqa: SLF001
        # A client that never reads: the writer can no longer send.
        async def never_sends(*args, **kwargs):
            await asyncio.sleep(3600)

        connection.websocket.send_json = never_sends
        for _ in range(connection.queue.maxsize + 5):
            hub.emit_to_user(B, {"type": "pong"})
        deadline = time.time() + 3
        while not connection.closed and time.time() < deadline:
            time.sleep(0.05)
        assert connection.closed  # dropped instead of growing without limit
        started = time.time()
        post(client, rid, actor=A, content="El destinatario no lee nada")
        assert time.time() - started < 2  # the sender never waited for the slow client
        stored = client.get(f"{CHATS}/{rid}/messages", headers=headers(A)).json()
        assert [m["text"] for m in stored] == ["El destinatario no lee nada"]


def test_the_rest_api_works_the_same_without_any_socket(live):
    client, *_ = live
    rid = match(client)
    sent = post(client, rid, actor=A, content="Sin nadie conectado")
    assert client.get(CHATS, headers=headers(B)).json()[0]["unreadCount"] == 1
    assert client.get(f"{CHATS}/{rid}/messages", headers=headers(B)).json()[0]["id"] == sent["id"]


def test_a_service_without_real_time_still_sends_messages(system):
    client, *_ = system  # built without a hub: events go nowhere and nothing breaks
    rid = match(client)
    assert post(client, rid, actor=A, content="Sin tiempo real")["text"] == "Sin tiempo real"
