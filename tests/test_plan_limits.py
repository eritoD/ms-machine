"""HU-54: límite de solicitudes de match del plan gratuito (6 cada 24 h), sin PostgreSQL."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.entitlements_client import EntitlementsClient, EntitlementsSettings, enforce, LimitePlanError
from app.services.matching_service import MatchingService
from test_matching import SECRET, Directory, headers

PREFIX = "/api/v1/matching"
TOKEN = "internal-token-for-tests-at-least-32-bytes-long"
GRATIS = {"plan": "GRATIS", "limites": {"solicitudes_match": {"maximo": 6, "ventana_horas": 24}}, "beneficios": []}
PRO = {"plan": "PRO", "limites": {"solicitudes_match": {"maximo": None, "ventana_horas": 24}},
       "beneficios": ["destacado"]}


class FakeRepository:
    def __init__(self):
        self.rows = []

    def ready(self):
        return None

    def pair_exists(self, a, b):
        return any({r["sender_id"], r["recipient_id"]} == {a, b} and r["status"] != "cancelled" for r in self.rows)

    def requests_sent_since(self, sender, since):
        return [r["created_at"] for r in self.rows if r["sender_id"] == sender and r["created_at"] > since]

    def request(self, sender, recipient):
        for row in self.rows:
            if {row["sender_id"], row["recipient_id"]} == {sender, recipient} and row["status"] != "cancelled":
                return row
        row = {"id": uuid4(), "sender_id": sender, "recipient_id": recipient, "status": "pending",
               "created_at": datetime.now(timezone.utc), "decided_at": None}
        self.rows.append(row)
        return row


class Payments:
    """ms-pago stand-in behind an httpx MockTransport."""

    def __init__(self, body=GRATIS, status=200):
        self.body, self.status, self.calls = body, status, 0

    def handler(self, request: httpx.Request):
        self.calls += 1
        if request.headers.get("X-Internal-Service-Token") != TOKEN:
            return httpx.Response(401, json={"detail": "service authentication required"})
        if self.status == "down":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(self.status, json=self.body)

    def client(self):
        settings = EntitlementsSettings("http://pago.test", TOKEN, 1, 60)
        return EntitlementsClient(settings, httpx.Client(base_url="http://pago.test",
                                                         transport=httpx.MockTransport(self.handler)))


def make(payments):
    directory = Directory()
    ids = [uuid4() for _ in range(10)]
    directory.active |= set(ids)
    repository = FakeRepository()
    service = MatchingService(repository, directory, entitlements=payments.client() if payments else None)
    settings = Settings("postgresql://unused", SECRET, "http://users.test", "sportmatch-auth", "sportmatch-mobile")
    return TestClient(create_app(service, settings)), repository, ids


def like(client, sender, recipient):
    return client.post(PREFIX + "/requests", headers=headers(sender), json={"recipient_id": str(recipient)})


def test_free_athlete_gets_402_on_the_seventh_request_in_24_hours():
    payments = Payments(GRATIS)
    client, repository, ids = make(payments)
    me, others = ids[0], ids[1:]
    for other in others[:6]:
        assert like(client, me, other).status_code == 200
    # Liking again someone already requested creates nothing new: allowed.
    assert like(client, me, others[0]).status_code == 200
    response = like(client, me, others[6])
    assert response.status_code == 402
    body = response.json()
    assert body["codigo"] == "LIMITE_PLAN" and body["limite"] == 6 and body["usado"] == 6
    oldest = min(r["created_at"] for r in repository.rows)
    assert datetime.fromisoformat(body["se_libera_en"].replace("Z", "+00:00")) == oldest + timedelta(hours=24)
    assert "Te quedan 0 de 6" in body["detail"]
    assert len(repository.rows) == 6
    assert payments.calls == 1  # cached for 60 s


def test_requests_older_than_24_hours_do_not_count():
    client, repository, ids = make(Payments(GRATIS))
    me = ids[0]
    for other in ids[1:7]:
        assert like(client, me, other).status_code == 200
    for row in repository.rows:
        row["created_at"] -= timedelta(hours=24, seconds=1)
    assert like(client, me, ids[7]).status_code == 200


def test_pro_athlete_has_no_limit():
    client, _, ids = make(Payments(PRO))
    for other in ids[1:]:
        assert like(client, ids[0], other).status_code == 200


@pytest.mark.parametrize("status", ["down", 503, 500])
def test_fail_open_when_ms_pago_is_down(status, caplog):
    client, _, ids = make(Payments(GRATIS, status=status))
    for other in ids[1:]:
        assert like(client, ids[0], other).status_code == 200
    assert "fail-open" in caplog.text


def test_without_entitlements_client_nothing_is_limited():
    client, _, ids = make(None)
    for other in ids[1:]:
        assert like(client, ids[0], other).status_code == 200


def test_wrong_internal_token_is_rejected_by_ms_pago_and_fails_open():
    payments = Payments(GRATIS)
    settings = EntitlementsSettings("http://pago.test", "otro-token-de-prueba-con-mas-de-32-bytes", 1, 60)
    client = EntitlementsClient(settings, httpx.Client(base_url="http://pago.test",
                                                       transport=httpx.MockTransport(payments.handler)))
    assert client.get("usuario", uuid4()) is None


def test_se_libera_en_uses_the_entry_that_exceeds_the_limit():
    payments = Payments({"plan": "GRATIS", "limites": {"x": {"maximo": 2, "ventana_horas": 24}}})
    now = datetime.now(timezone.utc)
    fechas = [now - timedelta(hours=h) for h in (20, 10, 5)]
    with pytest.raises(LimitePlanError) as error:
        enforce(payments.client(), "usuario", uuid4(), "x", lambda since: fechas, now=now)
    assert error.value.usado == 3
    assert error.value.se_libera_en == fechas[1] + timedelta(hours=24)


def test_after_a_connection_error_ms_pago_is_skipped_for_a_while():
    payments = Payments(GRATIS, status="down")
    now = [100.0]
    settings = EntitlementsSettings("http://pago.test", TOKEN, 1, 60)
    client = EntitlementsClient(settings, httpx.Client(base_url="http://pago.test",
                                                       transport=httpx.MockTransport(payments.handler)),
                                clock=lambda: now[0])
    assert client.get("usuario", uuid4()) is None and payments.calls == 1
    assert client.get("usuario", uuid4()) is None and payments.calls == 1  # backing off
    now[0] += 11
    payments.status = 200
    assert client.get("usuario", uuid4())["plan"] == "GRATIS" and payments.calls == 2


def test_plan_usage_counts_with_the_same_query_as_the_402():
    client, repository, ids = make(Payments(GRATIS))
    me = ids[0]
    for other in ids[1:4]:
        assert like(client, me, other).status_code == 200
    body = client.get(PREFIX + "/plan-usage", headers=headers(me)).json()
    assert body == {"plan": "GRATIS", "items": [{"recurso": "solicitudes_match", "limite": 6, "usado": 3,
                                                 "ventana_horas": 24, "se_libera_en": None}]}
    for other in ids[4:7]:
        like(client, me, other)
    full = client.get(PREFIX + "/plan-usage", headers=headers(me)).json()["items"][0]
    oldest = min(r["created_at"] for r in repository.rows)
    assert full["usado"] == 6
    assert datetime.fromisoformat(full["se_libera_en"].replace("Z", "+00:00")) == oldest + timedelta(hours=24)


def test_plan_usage_of_a_pro_or_unknown_plan():
    client, _, ids = make(Payments(PRO))
    like(client, ids[0], ids[1])
    item = client.get(PREFIX + "/plan-usage", headers=headers(ids[0])).json()["items"][0]
    assert (item["limite"], item["usado"]) == (None, 1)
    client, _, ids = make(Payments(GRATIS, status="down"))
    body = client.get(PREFIX + "/plan-usage", headers=headers(ids[0])).json()
    assert body["plan"] is None and body["items"][0]["usado"] is None
    assert client.get(PREFIX + "/plan-usage").status_code == 401
