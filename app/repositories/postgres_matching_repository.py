from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg import sql


# What a chat list shows for a message without text (an image or a location sent without caption).
_PREVIEW = """CASE WHEN m.message_type IS NULL THEN NULL
        WHEN m.message_type = 'TEXT' OR btrim(m.text) <> '' THEN m.text
        WHEN m.message_type = 'IMAGE' THEN 'Imagen' WHEN m.message_type = 'FILE' THEN 'Archivo'
        ELSE 'Ubicación' END"""


class PostgresMatchingRepository:
    def __init__(self, pool, schema="matching_api"):
        self.pool = pool
        self.schema = schema

    def execute(self, cursor, statement, params=None):
        query = sql.SQL(statement.replace("matching_api", "{schema}")).format(schema=sql.Identifier(self.schema))
        return cursor.execute(query, params)

    def migrate(self):
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('matching-api-schema', 0))")
            for migration in sorted((Path(__file__).parents[2] / "db/migrations").glob("*.sql")):
                self.execute(conn, migration.read_text())

    def ready(self):
        with self.pool.connection() as conn:
            self.execute(conn, "SELECT id FROM matching_api.requests LIMIT 1")

    def request(self, sender: UUID, recipient: UUID):
        if sender == recipient:
            raise HTTPException(422, "No puedes enviarte una solicitud.")
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # The unique unordered pair serializes simultaneous and reciprocal likes.
            row = None
            while row is None:
                self.execute(cur, """INSERT INTO matching_api.requests(id, sender_id, recipient_id)
                    VALUES (%s, %s, %s) ON CONFLICT (pair_low, pair_high) WHERE status <> 'cancelled' DO NOTHING
                    RETURNING *""", (uuid4(), sender, recipient))
                row = cur.fetchone()
                if row is None:
                    self.execute(cur, """SELECT * FROM matching_api.requests
                        WHERE pair_low=%s AND pair_high=%s AND status <> 'cancelled' FOR UPDATE""",
                                tuple(sorted((sender, recipient))))
                    row = cur.fetchone()
            if row["status"] == "rejected":
                raise HTTPException(409, "Esta solicitud ya se cerró.")
            return row

    def cancel(self, request_id: UUID, actor: UUID):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, "SELECT * FROM matching_api.requests WHERE id=%s FOR UPDATE", (request_id,))
            row = cur.fetchone()
            if row is None or actor not in (row["sender_id"], row["recipient_id"]):
                raise HTTPException(404, "Solicitud no disponible.")
            if actor != row["sender_id"]:
                raise HTTPException(403, "Solo quien envió la solicitud puede cancelarla.")
            if row["status"] == "cancelled":
                return
            if row["status"] != "pending":
                raise HTTPException(409, "La solicitud ya fue respondida. Actualiza tus matches.")
            self.execute(cur, """UPDATE matching_api.requests SET status='cancelled', decided_at=CURRENT_TIMESTAMP
                WHERE id=%s""", (request_id,))

    def get(self, request_id: UUID, actor: UUID):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, "SELECT * FROM matching_api.requests WHERE id=%s AND (sender_id=%s OR recipient_id=%s)",
                        (request_id, actor, actor))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "Conexión no disponible.")
            return row

    def decide(self, request_id: UUID, actor: UUID, decision: str):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, "SELECT * FROM matching_api.requests WHERE id=%s FOR UPDATE", (request_id,))
            row = cur.fetchone()
            if row is None or actor not in (row["sender_id"], row["recipient_id"]):
                raise HTTPException(404, "Solicitud no disponible.")
            if actor != row["recipient_id"]:
                raise HTTPException(403, "Solo quien recibe la solicitud puede responderla.")
            if row["status"] == decision:
                return row  # A network retry must not create a second match.
            if row["status"] != "pending":
                raise HTTPException(409, "La solicitud ya fue respondida.")
            self.execute(cur, """UPDATE matching_api.requests SET status=%s, decided_at=CURRENT_TIMESTAMP
                WHERE id=%s RETURNING *""", (decision, request_id))
            return cur.fetchone()

    def state(self, actor: UUID):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, f"""SELECT r.*, {_PREVIEW} AS last_message, m.created_at AS last_message_at
                FROM matching_api.requests r
                LEFT JOIN LATERAL (SELECT text, message_type, created_at FROM matching_api.messages
                    WHERE match_id=r.id ORDER BY id DESC LIMIT 1) m ON TRUE
                WHERE (sender_id=%s OR recipient_id=%s) AND status <> 'cancelled'
                ORDER BY COALESCE(m.created_at, r.decided_at, r.created_at) DESC, r.id""", (actor, actor))
            return cur.fetchall()

    def messages(self, match_id: UUID, after: int | None, before: int | None, limit: int, reader: UUID | None = None):
        """One page of messages. With a reader, the other person's messages up to the newest one
        returned are marked as read in the same transaction (opening the chat reads it). A message
        that arrives meanwhile has a higher id, so it stays unread until it is actually delivered."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            if after is not None:
                self.execute(cur, "SELECT * FROM matching_api.messages WHERE match_id=%s AND id>%s ORDER BY id LIMIT %s",
                            (match_id, after, limit))
                rows = cur.fetchall()
            else:
                self.execute(cur, """SELECT * FROM matching_api.messages WHERE match_id=%s
                    AND (%s::bigint IS NULL OR id<%s) ORDER BY id DESC LIMIT %s""", (match_id, before, before, limit))
                rows = list(reversed(cur.fetchall()))
            if reader is not None and rows:
                self.execute(cur, """UPDATE matching_api.messages SET read_at=CURRENT_TIMESTAMP
                    WHERE match_id=%s AND sender_id<>%s AND read_at IS NULL AND id<=%s RETURNING id, read_at""",
                            (match_id, reader, max(row["id"] for row in rows)))
                read_now = {row["id"]: row["read_at"] for row in cur.fetchall()}
                rows = [{**row, "read_at": read_now.get(row["id"], row["read_at"])} for row in rows]
            return rows

    def chats(self, actor: UUID, limit: int, offset: int):
        """Active conversations (accepted matches) of the person, newest activity first, each with
        its last message and how many messages from the other person are still unread."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, f"""SELECT r.id, r.sender_id, r.recipient_id,
                    {_PREVIEW} AS last_content, m.message_type AS last_type,
                    m.sender_id AS last_sender_id, m.created_at AS last_message_at,
                    (SELECT count(*) FROM matching_api.messages u
                        WHERE u.match_id=r.id AND u.sender_id<>%s AND u.read_at IS NULL) AS unread_count
                FROM matching_api.requests r
                LEFT JOIN LATERAL (SELECT text, message_type, sender_id, created_at FROM matching_api.messages
                    WHERE match_id=r.id ORDER BY id DESC LIMIT 1) m ON TRUE
                WHERE (r.sender_id=%s OR r.recipient_id=%s) AND r.status='accepted'
                ORDER BY COALESCE(m.created_at, r.decided_at, r.created_at) DESC, r.id
                LIMIT %s OFFSET %s""", (actor, actor, actor, limit, offset))
            return cur.fetchall()

    def unread_counts(self, user_id: UUID, match_id: UUID) -> tuple[int, int]:
        """(unread in this conversation, unread in all the person's active conversations)."""
        with self.pool.connection() as conn, conn.cursor() as cur:
            self.execute(cur, """SELECT count(*) FILTER (WHERE m.match_id=%s), count(*)
                FROM matching_api.messages m JOIN matching_api.requests r ON r.id=m.match_id
                WHERE r.status='accepted' AND (r.sender_id=%s OR r.recipient_id=%s)
                AND m.sender_id<>%s AND m.read_at IS NULL""", (match_id, user_id, user_id, user_id))
            return cur.fetchone()

    def mark_read(self, match_id: UUID, actor: UUID) -> int:
        """Mark every unread message the other person sent in this conversation as read."""
        with self.pool.connection() as conn, conn.cursor() as cur:
            self.execute(cur, """UPDATE matching_api.messages SET read_at=CURRENT_TIMESTAMP
                WHERE match_id=%s AND sender_id<>%s AND read_at IS NULL""", (match_id, actor))
            return cur.rowcount

    def send(self, match_id: UUID, actor: UUID, client_id: UUID, text: str, message_type: str = "TEXT",
             attachment_url: str | None = None, attachment_name: str | None = None, latitude: float | None = None,
             longitude: float | None = None, location_name: str | None = None):
        sent = {"message_type": message_type, "text": text, "attachment_url": attachment_url,
                "attachment_name": attachment_name, "latitude": latitude, "longitude": longitude,
                "location_name": location_name}
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Serializes messages per conversation so cursors cannot skip an uncommitted message.
            self.execute(cur, "SELECT * FROM matching_api.requests WHERE id=%s FOR UPDATE", (match_id,))
            match = cur.fetchone()
            if not match or actor not in (match["sender_id"], match["recipient_id"]) or match["status"] != "accepted":
                raise HTTPException(403, "El chat requiere un match aceptado.")
            self.execute(cur, """INSERT INTO matching_api.messages(match_id, sender_id, client_message_id, text,
                    message_type, attachment_url, attachment_name, latitude, longitude, location_name)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(match_id,sender_id,client_message_id) DO NOTHING RETURNING *""",
                        (match_id, actor, client_id, text, message_type, attachment_url, attachment_name,
                         latitude, longitude, location_name))
            row = cur.fetchone()
            created = row is not None
            if row is None:
                self.execute(cur, "SELECT * FROM matching_api.messages WHERE match_id=%s AND sender_id=%s AND client_message_id=%s",
                            (match_id, actor, client_id))
                row = cur.fetchone()
                if any(row[name] != value for name, value in sent.items()):
                    raise HTTPException(409, "El identificador del mensaje ya fue utilizado.")
            # `_created` tells a new message from the retry of one already saved (only the first is announced).
            return {**row, "_created": created}
