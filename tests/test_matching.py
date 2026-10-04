"""Real PostgreSQL tests in a disposable, isolated schema (never the app tables)."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg import sql

from app.core.config import Settings
from app.database.session import create_pool
from app.main import create_app
from app.repositories.postgres_matching_repository import PostgresMatchingRepository
from app.services.matching_service import MatchingService

SECRET = "matching-test-secret-longer-than-thirty-two-bytes"
A, B, C = uuid4(), uuid4(), uuid4()
PREFIX = "/api/v1/matching"


def token(user_id, **extra):
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + timedelta(minutes=10),
        "iss": "sportmatch-auth", "aud": "sportmatch-mobile", "token_type": "access", **extra}, SECRET, algorithm="HS256")


def headers(user_id):
    return {"Authorization": f"Bearer {token(user_id)}"}


class Directory:
    def __init__(self):
        self.active = {A, B, C}

    def card(self, user_id, _token):
        if user_id not in self.active:
            raise HTTPException(404, "Deportista no disponible")
        return {"user_id": str(user_id), "nombre": "Deportista", "apellido_inicial": "T.",
                "edad": None, "foto_perfil": None, "biografia": None, "deportes": [], "compatibilidad": 0}

    def cards(self, user_ids, token):
        return {uid: self.card(uid, token) for uid in user_ids if uid in self.active}


@pytest.fixture
def system():
    url = os.environ.get("MATCHING_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set MATCHING_TEST_DATABASE_URL to run isolated PostgreSQL integration tests")
    pool = create_pool(url)
    schema = "matching_test_" + uuid4().hex
    repository = PostgresMatchingRepository(pool, schema=schema)
    repository.migrate()
    directory = Directory()
    service = MatchingService(repository, directory)
    settings = Settings(url, SECRET, "http://users.test", "sportmatch-auth", "sportmatch-mobile")
    try:
        with TestClient(create_app(service, settings)) as client:
            yield client, repository, directory, settings
    finally:
        with pool.connection() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        pool.close()


def request(client, sender=A, recipient=B):
    response = client.post(PREFIX + "/requests", headers=headers(sender), json={"recipient_id": str(recipient)})
    assert response.status_code == 200, response.text
    return response.json()


def decide(client, request_id, actor=B, action="accept"):
    return client.post(f"{PREFIX}/requests/{request_id}/decision", headers=headers(actor), json={"action": action})


def send(client, match_id, actor=A, **body):
    return client.post(f"{PREFIX}/matches/{match_id}/messages", headers=headers(actor),
                       json={"text": "¡Hola!", "client_message_id": str(uuid4()), **body})


def test_like_is_pending_and_only_the_recipient_can_accept(system):
    client, _, _, _ = system
    pending = request(client)
    rid = pending["id"]
    assert pending["status"] == "pending"
    assert len(client.get(PREFIX + "/state", headers=headers(B)).json()["incoming"]) == 1
    assert len(client.get(PREFIX + "/state", headers=headers(A)).json()["outgoing"]) == 1
    assert client.get(f"{PREFIX}/matches/{rid}/messages", headers=headers(A)).status_code == 403
    assert send(client, rid).status_code == 403
    assert decide(client, rid, A).status_code == 403
    assert decide(client, rid, C).status_code == 404
    assert decide(client, rid).json()["status"] == "accepted"
    for actor in (A, B):
        state = client.get(PREFIX + "/state", headers=headers(actor)).json()
        assert len(state["matches"]) == 1
        assert state["incoming"] == state["outgoing"] == []


def test_rejection_creates_no_match_or_chat(system):
    client, _, _, _ = system
    rid = request(client)["id"]
    assert decide(client, rid, action="reject").json()["status"] == "rejected"
    assert decide(client, rid, action="reject").status_code == 200
    assert decide(client, rid).status_code == 409
    assert send(client, rid).status_code == 403
    for actor in (A, B):
        state = client.get(PREFIX + "/state", headers=headers(actor)).json()
        assert state["matches"] == state["incoming"] == state["outgoing"] == []


def test_reciprocal_likes_do_not_automatically_accept_and_are_unique(system):
    client, _, _, _ = system
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(lambda actor: request(client, actor, B if actor == A else A), [A, B, A, B]))
    assert len({row["id"] for row in rows}) == 1
    assert all(row["status"] == "pending" for row in rows)


def test_repeated_acceptance_is_idempotent(system):
    client, _, _, _ = system
    rid = request(client)["id"]
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: decide(client, rid), range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["decided_at"] for response in responses}) == 1
    assert len(client.get(PREFIX + "/state", headers=headers(A)).json()["matches"]) == 1


def test_conflicting_decisions_are_atomic(system):
    client, _, _, _ = system
    rid = request(client)["id"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda action: decide(client, rid, action=action), ["accept", "reject"]))
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_only_participants_can_read_and_send_persistent_messages(system):
    client, repository, directory, settings = system
    rid = request(client)["id"]
    decide(client, rid)
    assert send(client, rid, actor=C).status_code == 404
    assert client.get(f"{PREFIX}/matches/{rid}/messages", headers=headers(C)).status_code == 404
    first = send(client, rid).json()
    second = send(client, rid, actor=B, text="¡Entrenemos!").json()
    # A new application instance reads the same persisted match and messages.
    with TestClient(create_app(MatchingService(repository, directory), settings)) as restarted:
        response = restarted.get(f"{PREFIX}/matches/{rid}/messages", headers=headers(B))
        assert [m["id"] for m in response.json()] == [first["id"], second["id"]]
        assert response.json()[0]["sender_id"] == str(A)
        assert len(restarted.get(PREFIX + "/state", headers=headers(B)).json()["matches"]) == 1


def test_message_retry_and_cursor_pagination(system):
    client, _, _, _ = system
    rid = request(client)["id"]
    decide(client, rid)
    cid = str(uuid4())
    with ThreadPoolExecutor(max_workers=3) as executor:
        responses = list(executor.map(lambda _: send(client, rid, client_message_id=cid), range(3)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    assert send(client, rid, client_message_id=cid, text="Distinto").status_code == 409
    first = responses[0].json()
    second = send(client, rid, text="Segundo").json()
    path = f"{PREFIX}/matches/{rid}/messages"
    assert [m["id"] for m in client.get(path, headers=headers(A), params={"after_id": first["id"]}).json()] == [second["id"]]
    assert [m["id"] for m in client.get(path, headers=headers(A), params={"before_id": second["id"]}).json()] == [first["id"]]
    assert client.get(path, headers=headers(A), params={"before_id": 2, "after_id": 0}).status_code == 422
    assert client.get(path, headers=headers(A), params={"limit": 101}).status_code == 422
    assert send(client, rid, text="  ").status_code == 422
    assert send(client, rid, text="x" * 2001).status_code == 422


def test_authentication_actor_spoofing_and_unavailable_athletes(system):
    client, _, directory, _ = system
    assert client.get(PREFIX + "/state").status_code == 401
    for extra in ({"exp": 1}, {"aud": "other"}, {"token_type": "refresh"}):
        assert client.get(PREFIX + "/state", headers={"Authorization": f"Bearer {token(A, **extra)}"}).status_code == 401
    assert client.post(PREFIX + "/requests", headers=headers(A), json={"recipient_id": str(A)}).status_code == 422
    assert client.post(PREFIX + "/requests", headers=headers(A), json={"recipient_id": str(B), "sender_id": str(C)}).status_code == 422
    directory.active.remove(B)
    assert client.post(PREFIX + "/requests", headers=headers(A), json={"recipient_id": str(B)}).status_code == 404
    directory.active.remove(A)
    assert client.get(PREFIX + "/state", headers=headers(A)).status_code == 404


def test_deactivated_peer_cannot_be_contacted_even_after_acceptance(system):
    client, _, directory, _ = system
    rid = request(client)["id"]
    decide(client, rid)
    directory.active.remove(B)
    assert client.get(PREFIX + "/state", headers=headers(A)).json()["matches"] == []
    assert send(client, rid).status_code == 404


def cancel(client, request_id, actor=A):
    return client.delete(f"{PREFIX}/requests/{request_id}", headers=headers(actor))


def test_sender_can_cancel_and_resend_without_reviving_old_request(system):
    client, repository, _, _ = system
    rid = request(client)["id"]
    assert client.delete(f"{PREFIX}/requests/{rid}").status_code == 401
    assert cancel(client, rid, C).status_code == 404
    assert cancel(client, rid, B).status_code == 403
    assert cancel(client, rid).status_code == 204
    assert cancel(client, rid).status_code == 204
    assert repository.get(UUID(rid), A)["status"] == "cancelled"
    for actor in (A, B):
        assert client.get(PREFIX + "/state", headers=headers(actor)).json() == {
            "incoming": [], "outgoing": [], "matches": [], "hidden_user_ids": []}
    assert decide(client, rid).status_code == 409
    assert send(client, rid).status_code == 403
    new_id = request(client)["id"]
    assert new_id != rid
    # Retrying an old cancellation must never cancel a newer request.
    assert cancel(client, rid).status_code == 204
    assert client.get(PREFIX + "/state", headers=headers(B)).json()["incoming"][0]["id"] == new_id
    repository.migrate()  # Both migrations can safely run again at the next startup.
    assert decide(client, new_id).status_code == 200


@pytest.mark.parametrize("action", ["accept", "reject"])
def test_answered_request_cannot_be_cancelled(system, action):
    client, _, _, _ = system
    rid = request(client)["id"]
    assert decide(client, rid, action=action).status_code == 200
    assert cancel(client, rid).status_code == 409
    if action == "accept":
        assert send(client, rid).status_code == 200


def test_cancellation_and_acceptance_cannot_both_succeed(system):
    client, _, _, _ = system
    rid = request(client)["id"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        cancelling = executor.submit(cancel, client, rid)
        accepting = executor.submit(decide, client, rid)
        codes = (cancelling.result().status_code, accepting.result().status_code)
    assert codes in ((204, 409), (409, 200))
    matches = client.get(PREFIX + "/state", headers=headers(A)).json()["matches"]
    assert len(matches) == (1 if codes[1] == 200 else 0)


def test_accepted_match_exposes_only_the_peer_public_profile(system):
    client, _, _, _ = system
    rid = request(client)["id"]
    assert client.get(f"{PREFIX}/matches/{rid}", headers=headers(A)).status_code == 403
    decide(client, rid)
    for actor, peer in ((A, B), (B, A)):
        response = client.get(f"{PREFIX}/matches/{rid}", headers=headers(actor))
        assert response.status_code == 200
        profile = response.json()["athlete"]
        assert profile["user_id"] == str(peer)
        assert set(profile) == {"user_id", "nombre", "apellido_inicial", "edad", "foto_perfil", "biografia", "deportes", "compatibilidad"}
    assert client.get(f"{PREFIX}/matches/{rid}", headers=headers(C)).status_code == 404
