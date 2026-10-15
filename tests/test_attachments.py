"""HU-50: share a location, images and files in the chat. Real PostgreSQL in an isolated schema."""
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from app.main import create_app
from app.services.attachments import parse_hosts
from app.services.matching_service import MatchingService
from test_matching import A, B, C, PREFIX, decide, headers, request, send, system  # noqa: F401 (fixture)

CHATS = "/api/v1/chats"
HOSTS = ("files.sportmatch.test", "*.cdn.sportmatch.test")
PHOTO = "https://files.sportmatch.test/canchas/cancha-2.jpg"
PDF = "https://files.sportmatch.test/reglamento/torneo.pdf"
LOCATION = {"type": "LOCATION", "content": "Punto de encuentro en la cancha principal", "latitude": -33.5833,
            "longitude": -70.6333, "locationName": "Cancha 2 - Complejo Deportivo", "attachmentUrl": None}


@pytest.fixture
def chat(system):
    """The same system, with attachments enabled for HOSTS. `plain` is the client with them off."""
    plain, repository, directory, settings = system
    service = MatchingService(repository, directory, attachment_hosts=HOSTS)
    with TestClient(create_app(service, settings)) as client:
        yield client, plain, repository


def match(client, sender=A, recipient=B):
    rid = request(client, sender, recipient)["id"]
    assert decide(client, rid, actor=recipient).status_code == 200
    return rid


def post(client, rid, actor=A, **body):
    return client.post(f"{CHATS}/{rid}/messages", headers=headers(actor), json=body)


def sent(client, rid, actor=A, **body):
    response = post(client, rid, actor, **body)
    assert response.status_code == 200, response.text
    return response.json()


# -- location --------------------------------------------------------------------------------------


def test_location_message_with_the_payload_of_the_story(chat):
    client, *_ = chat
    rid = match(client)
    message = sent(client, rid, **LOCATION)
    assert message["message_type"] == "LOCATION"
    assert message["text"] == "Punto de encuentro en la cancha principal"
    assert (message["latitude"], message["longitude"]) == (-33.5833, -70.6333)
    assert message["location_name"] == "Cancha 2 - Complejo Deportivo"
    assert message["attachment_url"] is None and message["sender_id"] == str(A)
    # The other person reads it through either route.
    for path in (f"{CHATS}/{rid}/messages", f"{PREFIX}/matches/{rid}/messages"):
        [read] = client.get(path, headers=headers(B)).json()
        assert read["message_type"] == "LOCATION" and read["latitude"] == -33.5833 and read["id"] == message["id"]


def test_a_location_needs_only_coordinates_and_accepts_snake_case(chat):
    client, *_ = chat
    rid = match(client)
    bare = sent(client, rid, type="LOCATION", latitude=-33.4, longitude=-70.6)
    assert bare["text"] == "" and bare["location_name"] is None
    snake = sent(client, rid, type="LOCATION", latitude=1, longitude=2, location_name="Plaza", client_message_id=str(uuid4()))
    assert snake["location_name"] == "Plaza"


def test_coordinates_are_kept_to_six_decimals_and_edges_are_valid(chat):
    client, *_ = chat
    rid = match(client)
    precise = sent(client, rid, type="LOCATION", latitude=-33.583333333333, longitude=-70.633333333333)
    assert (precise["latitude"], precise["longitude"]) == (-33.583333, -70.633333)
    for lat, lon in ((90, 180), (-90, -180), (0, 0)):
        assert post(client, rid, type="LOCATION", latitude=lat, longitude=lon).status_code == 200


@pytest.mark.parametrize(
    "fields",
    [
        {"latitude": 90.1, "longitude": 0}, {"latitude": -91, "longitude": 0},
        {"latitude": 0, "longitude": 180.1}, {"latitude": 0, "longitude": -181},
        {"latitude": "NaN", "longitude": 0}, {"latitude": 0, "longitude": "Infinity"},
        {"latitude": "norte", "longitude": 0},
        {"latitude": 1}, {"longitude": 1}, {},
        {"latitude": 1, "longitude": 1, "attachmentUrl": PHOTO},
        {"latitude": 1, "longitude": 1, "attachmentName": "mapa.png"},
        {"latitude": 1, "longitude": 1, "locationName": "x" * 121},
        {"latitude": 1, "longitude": 1, "content": "x" * 2001},
    ],
)
def test_invalid_locations_are_rejected(chat, fields):
    client, *_ = chat
    rid = match(client)
    assert post(client, rid, type="LOCATION", **fields).status_code == 422
    assert client.get(f"{CHATS}/{rid}/messages", headers=headers(B)).json() == []


def test_an_infinite_coordinate_in_the_raw_json_is_rejected(chat):
    client, *_ = chat
    rid = match(client)
    response = client.post(f"{CHATS}/{rid}/messages", headers={**headers(A), "Content-Type": "application/json"},
                           content='{"type": "LOCATION", "latitude": 1e999, "longitude": 0}')
    assert response.status_code == 422


# -- images ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        PHOTO,
        "https://FILES.sportmatch.test/canchas/Cancha.PNG",
        "https://files.sportmatch.test:443/a.webp",
        "https://files.sportmatch.test/a.gif",
        "https://files.sportmatch.test/canchas/cancha.jpeg?X-Amz-Signature=abc&X-Amz-Expires=300",
        "https://a.cdn.sportmatch.test/x/y.WEBP",
        "https://files.sportmatch.test/cancha%20dos.png",
    ],
)
def test_images_from_the_trusted_storage_are_accepted(chat, url):
    client, *_ = chat
    rid = match(client)
    message = sent(client, rid, type="IMAGE", attachmentUrl=url)
    assert message["message_type"] == "IMAGE" and message["attachment_url"] == url
    assert message["text"] == "" and message["latitude"] is None


def test_an_image_can_carry_a_caption(chat):
    client, *_ = chat
    rid = match(client)
    message = sent(client, rid, type="IMAGE", attachmentUrl=PHOTO, content="Así se ve la cancha")
    assert message["text"] == "Así se ve la cancha"


@pytest.mark.parametrize(
    "url",
    [
        "http://files.sportmatch.test/a.png",  # not https
        "https://evil.example/a.png",  # not a trusted host
        "https://files.sportmatch.test.evil.example/a.png",
        "https://evilfiles.sportmatch.test/a.png",
        "https://cdn.sportmatch.test/a.png",  # '*.cdn...' is for subdomains only
        "https://evilcdn.sportmatch.test/a.png",
        "https://a.cdn.sportmatch.test.evil.example/a.png",
        "https://user:pass@files.sportmatch.test/a.png",
        "https://files.sportmatch.test@evil.example/a.png",
        "https://files.sportmatch.test:8443/a.png",
        "https://files.sportmatch.test:abc/a.png",
        "https://[::1]/a.png",
        "https://127.0.0.1/a.png",
        "https://files.sportmatch.test/a",  # no extension
        "https://files.sportmatch.test/a.exe",
        "https://files.sportmatch.test/a.svg",  # can carry scripts
        "https://files.sportmatch.test/a.html",
        "https://files.sportmatch.test/a.pdf",  # a file, not an image
        "https://files.sportmatch.test/a.png.exe",
        "https://files.sportmatch.test/dir.png/",  # a directory, not a file
        "https://files.sportmatch.test/dir.png/?x=a.png",
        "javascript:alert(1)",
        "data:image/png;base64,iVBORw0KGgo=",
        "file:///etc/passwd",
        "ftp://files.sportmatch.test/a.png",
        "//files.sportmatch.test/a.png",
        "files.sportmatch.test/a.png",
        "https://files.sportmatch.test/" + "a" * 2050 + ".png",
    ],
)
def test_unsafe_or_wrong_image_links_are_rejected(chat, url):
    client, *_ = chat
    rid = match(client)
    assert post(client, rid, type="IMAGE", attachmentUrl=url).status_code == 422
    assert client.get(f"{CHATS}/{rid}/messages", headers=headers(B)).json() == []


# -- files ---------------------------------------------------------------------------------------------


def test_a_file_takes_its_name_from_the_link_or_from_the_sender(chat):
    client, *_ = chat
    rid = match(client)
    derived = sent(client, rid, type="FILE", attachmentUrl=PDF)
    assert derived["message_type"] == "FILE" and derived["attachment_name"] == "torneo.pdf"
    named = sent(client, rid, type="FILE", attachmentUrl=PDF, attachmentName="Reglamento 2026.pdf", content="Léelo antes")
    assert named["attachment_name"] == "Reglamento 2026.pdf" and named["text"] == "Léelo antes"


@pytest.mark.parametrize("extension", ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv"])
def test_documents_are_accepted_as_files(chat, extension):
    client, *_ = chat
    rid = match(client)
    assert post(client, rid, type="FILE", attachmentUrl=f"https://files.sportmatch.test/a.{extension}").status_code == 200


@pytest.mark.parametrize("extension", ["exe", "zip", "html", "js", "svg", "png", "apk", "sh", "bat"])
def test_other_formats_are_not_files(chat, extension):
    client, *_ = chat
    rid = match(client)
    assert post(client, rid, type="FILE", attachmentUrl=f"https://files.sportmatch.test/a.{extension}").status_code == 422


@pytest.mark.parametrize("name", ["../secreto.pdf", "a/b.pdf", "a\\b.pdf", "con\x00nulo.pdf", "x" * 121])
def test_file_names_cannot_hide_paths(chat, name):
    client, *_ = chat
    rid = match(client)
    assert post(client, rid, type="FILE", attachmentUrl=PDF, attachmentName=name).status_code == 422


def test_attachments_are_off_until_hosts_are_configured(chat):
    _, plain, _ = chat  # the plain client has no CHAT_ATTACHMENT_HOSTS
    rid = match(plain)
    for fields in ({"type": "IMAGE", "attachmentUrl": PHOTO}, {"type": "FILE", "attachmentUrl": PDF}):
        response = post(plain, rid, **fields)
        assert response.status_code == 503
        assert response.json() == {"detail": "Los adjuntos no están habilitados en este momento."}
    assert post(plain, rid, type="LOCATION", latitude=1, longitude=2).status_code == 200  # no file involved
    assert post(plain, rid, type="TEXT", content="Hola").status_code == 200


# -- message shape ---------------------------------------------------------------------------------------


def test_text_through_the_new_route_and_the_old_one_coexist(chat):
    client, *_ = chat
    rid = match(client)
    new = sent(client, rid, content="Por la ruta nueva")  # type defaults to TEXT
    old = send(client, rid, text="Por la ruta antigua").json()
    assert new["message_type"] == "TEXT" == old["message_type"]
    assert [m["text"] for m in client.get(f"{CHATS}/{rid}/messages", headers=headers(B)).json()] == [
        "Por la ruta nueva", "Por la ruta antigua"]
    assert send(client, rid, text="  ").status_code == 422  # the old route still refuses empty text
    assert post(client, rid, type="TEXT", content="  ").status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"type": "TEXT"}, {"type": "TEXT", "content": "Hola", "attachmentUrl": PHOTO},
        {"type": "TEXT", "content": "Hola", "latitude": 1}, {"type": "TEXT", "content": "Hola", "locationName": "x"},
        {"type": "IMAGE"}, {"type": "IMAGE", "attachmentUrl": PHOTO, "latitude": 1},
        {"type": "IMAGE", "attachmentUrl": PHOTO, "locationName": "x"}, {"type": "IMAGE", "attachmentUrl": PHOTO, "attachmentName": "x.png"},
        {"type": "FILE"}, {"type": "FILE", "attachmentUrl": PDF, "longitude": 1},
        {"type": "VIDEO", "attachmentUrl": PHOTO}, {"type": "image", "attachmentUrl": PHOTO}, {"type": None},
        {"content": "Hola", "foo": "bar"}, {"content": "Hola", "clientMessageId": "no-es-uuid"},
        {"content": "x" * 2001}, {},
    ],
)
def test_a_message_carries_exactly_what_its_type_needs(chat, body):
    client, *_ = chat
    rid = match(client)
    assert post(client, rid, **body).status_code == 422
    assert client.get(f"{CHATS}/{rid}/messages", headers=headers(B)).json() == []


def test_retries_do_not_duplicate_and_conflicts_are_detected(chat):
    client, *_ = chat
    rid = match(client)
    cid = str(uuid4())
    first = sent(client, rid, clientMessageId=cid, **LOCATION)
    again = sent(client, rid, clientMessageId=cid, **LOCATION)
    assert again["id"] == first["id"]
    assert len(client.get(f"{CHATS}/{rid}/messages", headers=headers(B)).json()) == 1
    for changed in ({"latitude": -33.5834}, {"locationName": "Otro"}, {"type": "TEXT", "content": "x", "latitude": None,
                    "longitude": None, "locationName": None}, {"content": "Otro texto"}):
        assert post(client, rid, clientMessageId=cid, **{**LOCATION, **changed}).status_code in (409, 422)
    image_id = str(uuid4())
    sent(client, rid, clientMessageId=image_id, type="IMAGE", attachmentUrl=PHOTO)
    assert post(client, rid, clientMessageId=image_id, type="IMAGE", attachmentUrl=PHOTO.replace("-2", "-3")).status_code == 409
    # Without clientMessageId every send is a new message.
    one, two = sent(client, rid, content="Igual"), sent(client, rid, content="Igual")
    assert one["id"] != two["id"]


# -- who can send ------------------------------------------------------------------------------------------


def test_only_participants_of_an_accepted_match_can_send(chat):
    client, *_ = chat
    rid = match(client)
    assert post(client, rid, actor=C, **LOCATION).status_code == 404
    assert post(client, str(uuid4()), **LOCATION).status_code == 404
    assert client.post(f"{CHATS}/{rid}/messages", json=LOCATION).status_code == 401
    assert post(client, "no-es-uuid", **LOCATION).status_code == 422
    pending = request(client, B, C)["id"]
    assert post(client, pending, actor=B, **LOCATION).status_code == 403
    assert client.get(f"{CHATS}/{rid}/messages", headers=headers(B)).json() == []


# -- previews, unread and the migration ----------------------------------------------------------------------


def test_chat_list_shows_what_the_last_message_is(chat):
    client, *_ = chat
    rid = match(client)
    last = lambda: client.get(CHATS, headers=headers(B)).json()[0]["lastMessage"]  # noqa: E731
    assert client.get(CHATS, headers=headers(B)).json()[0]["lastMessage"] is None
    sent(client, rid, type="IMAGE", attachmentUrl=PHOTO)
    assert (last()["type"], last()["content"]) == ("IMAGE", "Imagen")
    sent(client, rid, type="IMAGE", attachmentUrl=PHOTO, content="La cancha")
    assert (last()["type"], last()["content"]) == ("IMAGE", "La cancha")
    sent(client, rid, type="FILE", attachmentUrl=PDF)
    assert (last()["type"], last()["content"]) == ("FILE", "Archivo")
    sent(client, rid, type="LOCATION", latitude=1, longitude=2)
    assert (last()["type"], last()["content"]) == ("LOCATION", "Ubicación")
    sent(client, rid, **{k: v for k, v in LOCATION.items() if k != "attachmentUrl"})
    assert last()["content"] == "Punto de encuentro en la cancha principal"
    sent(client, rid, content="Hola")
    assert (last()["type"], last()["content"]) == ("TEXT", "Hola")


def test_matching_state_shows_the_same_preview_and_nothing_for_an_empty_chat(chat):
    client, *_ = chat
    rid = match(client)
    state = lambda: client.get(PREFIX + "/state", headers=headers(B)).json()["matches"][0]["last_message"]  # noqa: E731
    assert state() is None  # an empty chat has no last message
    sent(client, rid, type="LOCATION", latitude=1, longitude=2)
    assert state() == "Ubicación"
    sent(client, rid, content="Llegué")
    assert state() == "Llegué"


def test_images_and_locations_count_as_unread_and_reading_marks_them(chat):
    client, *_ = chat
    rid = match(client)
    sent(client, rid, type="IMAGE", attachmentUrl=PHOTO)
    sent(client, rid, type="LOCATION", latitude=1, longitude=2)
    sent(client, rid, type="FILE", attachmentUrl=PDF)
    assert client.get(CHATS, headers=headers(B)).json()[0]["unreadCount"] == 3
    opened = client.get(f"{CHATS}/{rid}/messages", headers=headers(B)).json()
    assert [m["message_type"] for m in opened] == ["IMAGE", "LOCATION", "FILE"] and all(m["is_read"] for m in opened)
    assert client.get(CHATS, headers=headers(B)).json()[0]["unreadCount"] == 0


def test_the_database_enforces_the_shape_too(chat):
    client, _, repository = chat
    rid = match(client)
    insert = sql.SQL("""INSERT INTO {s}.messages(match_id, sender_id, client_message_id, text, message_type,
        attachment_url, latitude, longitude) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""").format(s=sql.Identifier(repository.schema))
    cases = {
        "image without link": ("", "IMAGE", None, None, None), "location without coordinates": ("", "LOCATION", None, None, None),
        "location with a link": ("", "LOCATION", PHOTO, 1, 2), "text with a link": ("Hola", "TEXT", PHOTO, None, None),
        "empty text": (" ", "TEXT", None, None, None), "unknown type": ("x", "VIDEO", None, None, None),
        "latitude out of range": ("", "LOCATION", None, 95, 2), "text over 2000": ("x" * 2001, "TEXT", None, None, None),
    }
    for name, (text, kind, url, lat, lon) in cases.items():
        with repository.pool.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, (rid, A, uuid4(), text, kind, url, lat, lon))
    with repository.pool.connection() as conn:  # and the valid empty-caption image goes in
        conn.execute(insert, (rid, A, uuid4(), "", "IMAGE", PHOTO, None, None))


def test_migration_keeps_old_messages_as_text_and_is_repeatable(system):
    _, repository, _, _ = system
    schema = "matching_old_" + repository.schema.split("_")[-1][:16]
    migrations = sorted((Path(__file__).parents[1] / "db/migrations").glob("*.sql"))
    old = type(repository)(repository.pool, schema=schema)
    try:
        with repository.pool.connection() as conn:
            for migration in [m for m in migrations if m.name < "005"]:  # the database before HU-50
                old.execute(conn, migration.read_text())
            rid, sender = uuid4(), uuid4()
            conn.execute(sql.SQL("""INSERT INTO {s}.requests(id, sender_id, recipient_id, status, decided_at)
                VALUES (%s, %s, %s, 'accepted', now())""").format(s=sql.Identifier(schema)), (rid, sender, uuid4()))
            conn.execute(sql.SQL("""INSERT INTO {s}.messages(match_id, sender_id, client_message_id, text)
                VALUES (%s, %s, %s, 'Mensaje antiguo')""").format(s=sql.Identifier(schema)), (rid, sender, uuid4()))
        old.migrate()
        old.migrate()
        with repository.pool.connection() as conn:
            row = conn.execute(sql.SQL("SELECT text, message_type, attachment_url, latitude FROM {s}.messages").format(
                s=sql.Identifier(schema))).fetchone()
            assert row == ("Mensaje antiguo", "TEXT", None, None)
            names = {r[0] for r in conn.execute(sql.SQL("""SELECT conname FROM pg_constraint WHERE contype = 'c'
                AND conrelid = to_regclass(%s)""").format(), (f'"{schema}".messages',)).fetchall()}
            assert {"messages_text_length", "messages_type_shape"} <= names  # created once, not duplicated
    finally:
        with repository.pool.connection() as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


# -- configuration -------------------------------------------------------------------------------------------


def test_attachment_hosts_setting():
    assert parse_hosts("") == () and parse_hosts(" , ") == ()
    assert parse_hosts("Files.SportMatch.cl, *.cdn.sportmatch.cl ,storage") == ("files.sportmatch.cl", "*.cdn.sportmatch.cl", "storage")
    for bad in ("https://files.sportmatch.cl", "files.sportmatch.cl/path", "*", "a b", "*.", "-x.cl", "a..cl"):
        with pytest.raises(RuntimeError, match="CHAT_ATTACHMENT_HOSTS"):
            parse_hosts(bad)
