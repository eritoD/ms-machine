"""HU-49: chat list with unread counters. Real PostgreSQL in an isolated schema (see test_matching)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import sql

from test_matching import A, B, C, PREFIX, decide, headers, request, send, system  # noqa: F401 (fixture)

CHATS = "/api/v1/chats"


def match(client, sender=A, recipient=B):
    rid = request(client, sender, recipient)["id"]
    assert decide(client, rid, actor=recipient).status_code == 200
    return rid


def chat_list(client, actor, **params):
    response = client.get(CHATS, headers=headers(actor), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def unread(client, actor, conversation_id):
    return next(c["unreadCount"] for c in chat_list(client, actor) if c["conversationId"] == conversation_id)


# -- the list ---------------------------------------------------------------------------------------


def test_chat_list_has_the_agreed_shape(system):
    client, *_ = system
    rid = match(client)
    send(client, rid, actor=B, text="Nos vemos en la cancha")
    [chat] = chat_list(client, A)
    assert set(chat) == {"conversationId", "participant", "lastMessage", "unreadCount"}
    assert chat["conversationId"] == rid
    assert chat["participant"] == {"userId": str(B), "name": "Deportista T.", "avatarUrl": None}
    assert set(chat["lastMessage"]) == {"content", "senderId", "createdAt", "type"}
    assert chat["lastMessage"]["type"] == "TEXT"
    assert chat["lastMessage"]["content"] == "Nos vemos en la cancha"
    assert chat["lastMessage"]["senderId"] == str(B)
    assert chat["lastMessage"]["createdAt"].endswith("Z")  # UTC, like the contract's example
    assert datetime.fromisoformat(chat["lastMessage"]["createdAt"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    assert chat["unreadCount"] == 1


def test_the_participant_is_always_the_other_person(system):
    client, *_ = system
    match(client, A, B)
    assert chat_list(client, A)[0]["participant"]["userId"] == str(B)
    assert chat_list(client, B)[0]["participant"]["userId"] == str(A)


def test_a_match_without_messages_is_listed_with_nothing_unread(system):
    client, *_ = system
    rid = match(client)
    [chat] = chat_list(client, B)
    assert chat["conversationId"] == rid and chat["lastMessage"] is None and chat["unreadCount"] == 0


def test_only_active_conversations_where_the_person_takes_part_are_listed(system):
    client, *_ = system
    assert chat_list(client, A) == []
    request(client, A, B)  # pending
    rejected = request(client, B, C)["id"]
    decide(client, rejected, actor=C, action="reject")
    cancelled = request(client, C, A)["id"]
    client.delete(f"{PREFIX}/requests/{cancelled}", headers=headers(C))
    assert chat_list(client, A) == [] and chat_list(client, B) == [] and chat_list(client, C) == []
    active = match(client, A, C)
    assert [c["conversationId"] for c in chat_list(client, A)] == [active]
    assert [c["conversationId"] for c in chat_list(client, C)] == [active]
    assert chat_list(client, B) == []  # not a participant


def test_chats_are_ordered_by_their_last_message_newest_first(system):
    client, *_ = system
    with_b = match(client, A, B)
    with_c = match(client, A, C)
    assert [c["conversationId"] for c in chat_list(client, A)] == [with_c, with_b]  # by match time
    send(client, with_b, actor=A, text="Primero")
    assert [c["conversationId"] for c in chat_list(client, A)] == [with_b, with_c]
    send(client, with_c, actor=C, text="Segundo")
    assert [c["conversationId"] for c in chat_list(client, A)] == [with_c, with_b]
    send(client, with_b, actor=B, text="Tercero")
    assert [c["conversationId"] for c in chat_list(client, A)] == [with_b, with_c]
    assert chat_list(client, A)[0]["lastMessage"]["content"] == "Tercero"


def test_a_deactivated_person_is_left_out(system):
    client, _, directory, _ = system
    rid = match(client)
    assert len(chat_list(client, A)) == 1
    directory.active.discard(B)
    assert chat_list(client, A) == []


def test_pagination_and_validation(system):
    client, *_ = system
    with_b, with_c = match(client, A, B), match(client, A, C)
    assert [c["conversationId"] for c in chat_list(client, A, limit=1)] == [with_c]
    assert [c["conversationId"] for c in chat_list(client, A, limit=1, offset=1)] == [with_b]
    assert chat_list(client, A, offset=2) == []
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "x"}):
        assert client.get(CHATS, headers=headers(A), params=params).status_code == 422


def test_the_list_needs_a_session(system):
    client, *_ = system
    assert client.get(CHATS).status_code == 401
    assert client.get(CHATS, headers={"Authorization": "Bearer nope"}).status_code == 401


# -- unread counters ---------------------------------------------------------------------------------


def test_unread_counts_only_the_other_persons_messages(system):
    client, *_ = system
    rid = match(client)
    for _ in range(3):
        send(client, rid, actor=A)
    send(client, rid, actor=B, text="Respuesta")
    assert unread(client, B, rid) == 3  # what A sent
    assert unread(client, A, rid) == 1  # what B sent; A's own messages never count


def test_opening_the_conversation_marks_it_read_and_resets_the_counter(system):
    client, *_ = system
    rid = match(client)
    sent = [send(client, rid, actor=A, text=f"Mensaje {n}").json() for n in range(3)]
    assert unread(client, B, rid) == 3

    opened = client.get(f"{CHATS}/{rid}/messages", headers=headers(B))
    assert opened.status_code == 200
    assert [m["id"] for m in opened.json()] == [m["id"] for m in sent]
    assert all(m["is_read"] and m["read_at"] for m in opened.json())  # as returned, already read
    assert unread(client, B, rid) == 0

    # A, who sent them, now sees them as read: a read receipt.
    mine = client.get(f"{CHATS}/{rid}/messages", headers=headers(A)).json()
    assert all(m["is_read"] for m in mine)
    assert unread(client, A, rid) == 0


def test_the_existing_matching_route_also_marks_messages_read(system):
    client, *_ = system
    rid = match(client)
    send(client, rid, actor=A)
    assert unread(client, B, rid) == 1
    assert client.get(f"{PREFIX}/matches/{rid}/messages", headers=headers(B)).status_code == 200
    assert unread(client, B, rid) == 0


def test_reading_never_marks_your_own_messages_or_the_other_persons_view(system):
    client, *_ = system
    rid = match(client)
    send(client, rid, actor=A)
    mine = client.get(f"{CHATS}/{rid}/messages", headers=headers(A)).json()
    assert mine[0]["is_read"] is False and mine[0]["read_at"] is None  # B has not read it
    assert unread(client, B, rid) == 1  # A opening the chat did not read it for B


def test_only_what_was_delivered_gets_marked(system):
    client, *_ = system
    rid = match(client)
    ids = [send(client, rid, actor=A, text=f"M{n}").json()["id"] for n in range(3)]
    path = f"{CHATS}/{rid}/messages"
    # Older history up to (not including) the last message: the last one stays unread.
    older = client.get(path, headers=headers(B), params={"before_id": ids[2]}).json()
    assert [m["id"] for m in older] == ids[:2]
    assert unread(client, B, rid) == 1
    # A poll that finds nothing new marks nothing.
    send(client, rid, actor=A, text="Nuevo")
    assert client.get(path, headers=headers(B), params={"after_id": 10**9}).json() == []
    assert unread(client, B, rid) == 2
    # Fetching the new ones reads them.
    new = client.get(path, headers=headers(B), params={"after_id": ids[2]}).json()
    assert [m["text"] for m in new] == ["Nuevo"]
    assert unread(client, B, rid) == 0


def test_explicit_mark_as_read(system):
    client, *_ = system
    rid = match(client)
    for _ in range(4):
        send(client, rid, actor=A)
    response = client.patch(f"{CHATS}/{rid}/read", headers=headers(B))
    assert response.status_code == 200
    assert response.json() == {"conversationId": rid, "markedAsRead": 4, "unreadCount": 0}
    assert unread(client, B, rid) == 0
    again = client.patch(f"{CHATS}/{rid}/read", headers=headers(B))
    assert again.json() == {"conversationId": rid, "markedAsRead": 0, "unreadCount": 0}  # idempotent
    # It reads what the OTHER person sent, never the caller's own messages.
    send(client, rid, actor=B, text="Mío")
    assert client.patch(f"{CHATS}/{rid}/read", headers=headers(B)).json()["markedAsRead"] == 0
    assert unread(client, A, rid) == 1


def test_a_message_arriving_during_the_reads_is_never_marked_unseen(system):
    """Senders and readers run together. Whatever B was handed by a read may be marked read;
    anything newer than the newest message B was ever handed must still be unread."""
    client, *_ = system
    rid = match(client)
    delivered = []

    def send_and_read(n):
        send(client, rid, actor=A, text=f"M{n}")
        page = client.get(f"{CHATS}/{rid}/messages", headers=headers(B), params={"limit": 100}).json()
        delivered.extend(int(m["id"]) for m in page)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(send_and_read, range(24)))
    everything = client.get(f"{CHATS}/{rid}/messages", headers=headers(A), params={"limit": 100}).json()
    assert len(everything) == 24
    newest_delivered = max(delivered)
    for message in everything:  # A's view tells whether B read each message
        if int(message["id"]) > newest_delivered:
            assert message["is_read"] is False, "marked read although it was never delivered"
        else:
            assert message["is_read"] is True
    # The read that follows catches up with everything.
    client.get(f"{CHATS}/{rid}/messages", headers=headers(B), params={"limit": 100})
    assert unread(client, B, rid) == 0


# -- who can read what ----------------------------------------------------------------------------------


def test_only_participants_of_an_accepted_match_can_open_or_mark_a_chat(system):
    client, *_ = system
    rid = match(client)
    send(client, rid, actor=A)
    for method, path in (("get", f"{CHATS}/{rid}/messages"), ("patch", f"{CHATS}/{rid}/read")):
        assert getattr(client, method)(path, headers=headers(C)).status_code == 404
        assert getattr(client, method)(path).status_code == 401
        assert getattr(client, method)(path.replace(rid, str(uuid4())), headers=headers(A)).status_code == 404
        assert getattr(client, method)(path.replace(rid, "no-es-uuid"), headers=headers(A)).status_code == 422
    assert unread(client, B, rid) == 1  # nothing was read by the outsider's attempts
    pending = request(client, B, C)["id"]
    for method, path in (("get", f"{CHATS}/{pending}/messages"), ("patch", f"{CHATS}/{pending}/read")):
        assert getattr(client, method)(path, headers=headers(B)).status_code == 403  # not accepted yet


def test_the_messages_route_keeps_the_cursor_rules(system):
    client, *_ = system
    rid = match(client)
    path = f"{CHATS}/{rid}/messages"
    assert client.get(path, headers=headers(A), params={"before_id": 2, "after_id": 0}).status_code == 422
    assert client.get(path, headers=headers(A), params={"limit": 101}).status_code == 422


# -- the migration -------------------------------------------------------------------------------------------


def test_migration_starts_old_messages_as_read_and_is_repeatable(system):
    client, repository, _, _ = system
    schema = "matching_old_" + repository.schema.split("_")[-1][:16]
    migrations = sorted((Path(__file__).parents[1] / "db/migrations").glob("*.sql"))
    before = [m for m in migrations if m.name < "004"]
    old = type(repository)(repository.pool, schema=schema)
    try:
        with repository.pool.connection() as conn:
            for migration in before:  # the database as it was before HU-49
                old.execute(conn, migration.read_text())
            rid, sender = uuid4(), uuid4()
            conn.execute(sql.SQL("""INSERT INTO {s}.requests(id, sender_id, recipient_id, status, decided_at)
                VALUES (%s, %s, %s, 'accepted', now())""").format(s=sql.Identifier(schema)), (rid, sender, uuid4()))
            conn.execute(sql.SQL("""INSERT INTO {s}.messages(match_id, sender_id, client_message_id, text)
                VALUES (%s, %s, %s, 'Antiguo')""").format(s=sql.Identifier(schema)), (rid, sender, uuid4()))
        old.migrate()
        old.migrate()  # twice: the guard keeps new unread messages unread
        with repository.pool.connection() as conn:
            assert conn.execute(sql.SQL("SELECT read_at IS NOT NULL FROM {s}.messages").format(s=sql.Identifier(schema))).fetchone()[0]
            conn.execute(sql.SQL("""INSERT INTO {s}.messages(match_id, sender_id, client_message_id, text)
                VALUES (%s, %s, %s, 'Nuevo')""").format(s=sql.Identifier(schema)), (rid, sender, uuid4()))
        old.migrate()
        with repository.pool.connection() as conn:
            states = conn.execute(sql.SQL("SELECT text, read_at IS NULL FROM {s}.messages ORDER BY id").format(s=sql.Identifier(schema))).fetchall()
        assert states == [("Antiguo", False), ("Nuevo", True)]
    finally:
        with repository.pool.connection() as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
