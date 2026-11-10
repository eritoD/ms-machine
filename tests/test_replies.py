"""HU-64: answer a specific message of the same chat. Real PostgreSQL in an isolated schema."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from app.main import create_app
from app.services.matching_service import MatchingService
from app.services.realtime import RealtimeHub
from test_matching import A, B, C, decide, headers, request, system  # noqa: F401 (fixture)
from test_realtime import socket

CHATS = "/api/v1/chats"
MATCHING = "/api/v1/matching/matches"
GONE = "El mensaje citado no está disponible."


def match(client, sender=A, recipient=B):
    rid = request(client, sender, recipient)["id"]
    assert decide(client, rid, actor=recipient).status_code == 200
    return rid


def post(client, rid, actor=A, text="hola", **body):
    return client.post(f"{CHATS}/{rid}/messages", headers=headers(actor),
                       json={"type": "TEXT", "content": text, "clientMessageId": str(uuid4()), **body})


def sent(client, rid, actor=A, text="hola", **body):
    response = post(client, rid, actor, text, **body)
    assert response.status_code == 200, response.text
    return response.json()


def history(client, rid, actor=A, **params):
    response = client.get(f"{CHATS}/{rid}/messages", headers=headers(actor), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def rows(repository, statement, *params):
    with repository.pool.connection() as conn:
        cursor = conn.execute(sql.SQL(statement.replace("matching_api", "{s}")).format(
            s=sql.Identifier(repository.schema)), params)
        return cursor.fetchall() if cursor.description else []


def test_a_message_without_reply_has_no_reply_fields(system):
    client, *_ = system
    rid = match(client)
    message = sent(client, rid)
    assert message["reply_to_id"] is None and message["reply_to"] is None


def test_answering_the_other_persons_message_and_your_own(system):
    client, *_ = system
    rid = match(client)
    theirs = sent(client, rid, B, "¿Entrenamos mañana?")
    reply = sent(client, rid, A, "Sí, a las 7", replyTo=theirs["id"])
    assert reply["reply_to_id"] == theirs["id"]
    assert reply["reply_to"] == {"id": theirs["id"], "sender_id": str(B), "text": "¿Entrenamos mañana?",
                                 "message_type": "TEXT", "available": True}
    mine = sent(client, rid, A, "Llevo balón", replyTo=reply["id"])      # your own message
    assert mine["reply_to"]["sender_id"] == str(A) and mine["reply_to"]["text"] == "Sí, a las 7"


def test_both_spellings_and_the_old_route_accept_the_reply(system):
    client, *_ = system
    rid = match(client)
    original = sent(client, rid, B, "base")
    assert sent(client, rid, A, replyTo=original["id"])["reply_to_id"] == original["id"]
    assert sent(client, rid, A, reply_to=original["id"])["reply_to_id"] == original["id"]
    old = client.post(f"{MATCHING}/{rid}/messages", headers=headers(A),
                      json={"text": "ok", "client_message_id": str(uuid4()), "reply_to": int(original["id"])})
    assert old.status_code == 200 and old.json()["reply_to"]["text"] == "base"


def test_answering_an_answer_quotes_only_that_answer(system):
    client, *_ = system
    rid = match(client)
    first = sent(client, rid, B, "uno")
    second = sent(client, rid, A, "dos", replyTo=first["id"])
    third = sent(client, rid, B, "tres", replyTo=second["id"])
    assert third["reply_to"]["text"] == "dos" and third["reply_to"]["id"] == second["id"]
    assert set(third["reply_to"]) == {"id", "sender_id", "text", "message_type", "available"}   # no nested quote


def test_the_preview_is_cut_short_and_says_what_kind_of_message_it_is(system):
    client, *_ = system
    rid = match(client)
    long = sent(client, rid, B, "x" * 1500)
    preview = sent(client, rid, A, replyTo=long["id"])["reply_to"]
    assert preview["text"] == "x" * 100 + "…" and len(preview["text"]) == 101
    place = client.post(f"{CHATS}/{rid}/messages", headers=headers(B), json={
        "type": "LOCATION", "latitude": -33.4, "longitude": -70.6, "clientMessageId": str(uuid4())}).json()
    preview = sent(client, rid, A, replyTo=place["id"])["reply_to"]
    assert preview["message_type"] == "LOCATION" and preview["text"] == ""   # the app labels it, no coordinates leak
    assert "latitude" not in preview and "-33.4" not in str(preview)


def test_quoting_a_message_of_another_chat_or_one_that_does_not_exist_is_refused_the_same_way(system):
    client, repository, *_ = system
    rid, other = match(client, A, B), match(client, A, C)
    secret = sent(client, other, C, "esto es de otro chat")
    before = rows(repository, "SELECT count(*) FROM matching_api.messages")
    foreign = post(client, rid, A, replyTo=secret["id"])
    missing = post(client, rid, A, replyTo=987654321)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": GONE}
    assert "otro chat" not in foreign.text
    assert rows(repository, "SELECT count(*) FROM matching_api.messages") == before          # nothing was saved


def test_the_database_itself_refuses_a_reply_across_chats(system):
    client, repository, *_ = system
    rid, other = match(client, A, B), match(client, A, C)
    secret = sent(client, other, C, "otro")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        rows(repository, """INSERT INTO matching_api.messages(match_id, sender_id, client_message_id, text, reply_to_id, is_reply)
            VALUES (%s, %s, %s, 'x', %s, TRUE)""", rid, A, uuid4(), int(secret["id"]))


@pytest.mark.parametrize("value", [0, -1, "abc", 1.5, 2 ** 63])
def test_a_malformed_reply_id_is_422(system, value):
    client, *_ = system
    rid = match(client)
    assert post(client, rid, A, replyTo=value).status_code == 422


def test_only_the_two_participants_with_a_session_can_answer(system):
    client, *_ = system
    rid = match(client)
    original = sent(client, rid, B)
    assert post(client, rid, C, replyTo=original["id"]).status_code == 404          # not in the chat: it does not exist for them
    assert client.post(f"{CHATS}/{rid}/messages", json={"content": "x", "replyTo": original["id"]}).status_code == 401
    assert client.get(f"{CHATS}/{rid}/messages", params={"around_id": original["id"]}).status_code == 401
    assert client.get(f"{CHATS}/{rid}/messages", headers=headers(C), params={"around_id": original["id"]}).status_code == 404


def test_a_blocked_or_unavailable_person_cannot_be_answered_and_the_rule_is_unchanged(system):
    client, _, directory, _ = system
    rid = match(client)
    original = sent(client, rid, B)
    directory.active.discard(B)          # what a block (or a deleted account) looks like to this service: no card
    assert post(client, rid, A, replyTo=original["id"]).status_code == 404
    assert post(client, rid, A).status_code == 404       # exactly like a message that is not a reply


def test_the_history_carries_every_preview_without_a_query_per_message(system):
    client, repository, *_ = system
    rid = match(client)
    base = sent(client, rid, B, "base")
    for number in range(30):
        sent(client, rid, A, f"r{number}", replyTo=base["id"])
    statements = []
    original = repository.execute
    repository.execute = lambda cursor, statement, params=None: (statements.append(statement), original(cursor, statement, params))[1]
    try:
        page = history(client, rid, A, limit=50)
    finally:
        repository.execute = original
    assert len(page) == 31 and all(m["reply_to"]["text"] == "base" for m in page[1:])
    assert len(statements) <= 4, statements                         # the same count with 1 or 30 replies


def test_a_removed_original_leaves_the_reply_and_says_it_is_not_available(system):
    client, repository, *_ = system
    rid = match(client)
    original = sent(client, rid, B, "texto privado del original")
    reply = sent(client, rid, A, "respuesta", replyTo=original["id"])
    rows(repository, "DELETE FROM matching_api.messages WHERE id = %s", int(original["id"]))
    [saved] = history(client, rid, A)
    assert saved["id"] == reply["id"] and saved["text"] == "respuesta"
    assert saved["reply_to_id"] is None
    assert saved["reply_to"] == {"id": None, "sender_id": None, "text": None, "message_type": None, "available": False}
    assert "privado" not in str(saved)
    assert post(client, rid, A, replyTo=original["id"]).status_code == 404       # and it cannot be quoted anymore


def test_loading_around_a_quoted_message(system):
    client, *_ = system
    rid = match(client)
    ids = [sent(client, rid, A if n % 2 else B, f"m{n}")["id"] for n in range(120)]
    target = ids[20]
    page = history(client, rid, A, around_id=target, limit=20)
    got = [int(m["id"]) for m in page]
    assert got == sorted(got) and len(got) == 20 and int(target) in got
    assert got[0] == int(ids[11]) and got[-1] == int(ids[30])        # centred on it
    near_start = history(client, rid, A, around_id=ids[1], limit=20)
    assert int(near_start[0]["id"]) == int(ids[0]) and len(near_start) == 12       # the start of the chat: only what exists before
    near_end = history(client, rid, A, around_id=ids[-1], limit=20)
    assert int(near_end[-1]["id"]) == int(ids[-1]) and len(near_end) == 10        # the end: only what exists
    assert len(history(client, rid, A, around_id=target, limit=100)) == 71        # 21 before it and 50 after: never above the limit


def test_around_needs_a_message_of_this_chat_and_a_single_cursor(system):
    client, *_ = system
    rid, other = match(client, A, B), match(client, A, C)
    mine, foreign = sent(client, rid, A), sent(client, other, A)
    for value in (foreign["id"], 987654321):
        assert client.get(f"{CHATS}/{rid}/messages", headers=headers(A), params={"around_id": value}).status_code == 404
    for extra in ({"before_id": mine["id"]}, {"after_id": 1}):
        both = client.get(f"{CHATS}/{rid}/messages", headers=headers(A), params={"around_id": mine["id"], **extra})
        assert both.status_code == 422
    for bad in (0, -3, "x", 101):
        key = "limit" if bad == 101 else "around_id"
        params = {"around_id": mine["id"], "limit": 101} if key == "limit" else {"around_id": bad}
        assert client.get(f"{CHATS}/{rid}/messages", headers=headers(A), params=params).status_code == 422
    assert client.get(f"{MATCHING}/{rid}/messages", headers=headers(A), params={"around_id": mine["id"]}).status_code == 200


def test_around_marks_read_up_to_the_newest_delivered_and_no_further(system):
    client, *_ = system
    rid = match(client)
    ids = [sent(client, rid, B, f"b{n}")["id"] for n in range(10)]
    history(client, rid, A, around_id=ids[2], limit=4)                 # delivers b1..b4: read up to b4, like any page
    unread = client.get(CHATS, headers=headers(A)).json()[0]["unreadCount"]
    assert unread == 5                                                  # b5..b9 stay unread


def test_a_retry_of_a_reply_returns_the_same_message_and_a_different_quote_is_a_conflict(system):
    client, repository, *_ = system
    rid = match(client)
    one, two = sent(client, rid, B, "uno"), sent(client, rid, B, "dos")
    key = str(uuid4())
    body = {"type": "TEXT", "content": "ok", "clientMessageId": key, "replyTo": one["id"]}
    first = client.post(f"{CHATS}/{rid}/messages", headers=headers(A), json=body).json()
    again = client.post(f"{CHATS}/{rid}/messages", headers=headers(A), json=body).json()
    assert first["id"] == again["id"] and again["reply_to"]["text"] == "uno"
    changed = client.post(f"{CHATS}/{rid}/messages", headers=headers(A), json={**body, "replyTo": two["id"]})
    assert changed.status_code == 409
    assert rows(repository, "SELECT count(*) FROM matching_api.messages WHERE match_id = %s", rid)[0][0] == 3


def test_two_simultaneous_answers_to_the_same_message_are_both_saved(system):
    client, repository, *_ = system
    rid = match(client)
    original = sent(client, rid, B, "base")

    def answer(who):
        return post(client, rid, who, text=f"de {who}", replyTo=original["id"])

    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(answer, (A, B))
    assert first.status_code == second.status_code == 200 and first.json()["id"] != second.json()["id"]
    assert rows(repository, "SELECT count(*) FROM matching_api.messages WHERE reply_to_id = %s", int(original["id"]))[0][0] == 2


def test_an_answer_notifies_like_any_message_and_never_carries_the_text(system):
    client, repository, *_ = system
    rid = match(client)
    original = sent(client, rid, B, "secreto del original")
    before = rows(repository, "SELECT count(*) FROM matching_api.notification_outbox WHERE kind = 'mensaje'")[0][0]
    sent(client, rid, A, "secreto de la respuesta", replyTo=original["id"])
    notices = rows(repository, "SELECT user_id, title, body, data FROM matching_api.notification_outbox "
                               "WHERE kind = 'mensaje' ORDER BY id")
    assert len(notices) == before + 1
    user, title, body, data = notices[-1]
    assert user == B and (title, body) == ("Nuevo mensaje", "Tienes un mensaje de uno de tus matches.")
    assert data == {"screen": "chat", "matchId": rid} and "secreto" not in str(notices[-1])


def test_the_migration_can_run_again_and_keeps_old_messages(system):
    client, repository, *_ = system
    rid = match(client)
    sent(client, rid, A, "antes")
    repository.migrate()
    repository.migrate()
    constraints = rows(repository, "SELECT count(*) FROM pg_constraint WHERE conname = 'messages_reply_same_chat' "
                                   "AND conrelid = to_regclass('matching_api.messages')")
    assert constraints[0][0] == 1
    assert [m["text"] for m in history(client, rid, A)] == ["antes"]


def test_the_new_message_event_of_a_reply_carries_the_preview(system):
    _, repository, directory, settings = system
    hub = RealtimeHub()
    service = MatchingService(repository, directory, realtime=hub)
    with TestClient(create_app(service, settings)) as client:
        rid = match(client)
        original = sent(client, rid, B, "base")
        with socket(client, B, join=rid) as ws:
            reply = sent(client, rid, A, "respuesta", replyTo=original["id"])
            event = ws.receive_json()
    assert event["type"] == "new_message" and event["message"]["id"] == reply["id"]
    assert event["message"]["reply_to"] == {"id": original["id"], "sender_id": str(B), "text": "base",
                                            "message_type": "TEXT", "available": True}
