from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg import sql


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
            self.execute(cur, """SELECT r.*, m.text AS last_message, m.created_at AS last_message_at
                FROM matching_api.requests r
                LEFT JOIN LATERAL (SELECT text, created_at FROM matching_api.messages
                    WHERE match_id=r.id ORDER BY id DESC LIMIT 1) m ON TRUE
                WHERE (sender_id=%s OR recipient_id=%s) AND status <> 'cancelled'
                ORDER BY COALESCE(m.created_at, r.decided_at, r.created_at) DESC, r.id""", (actor, actor))
            return cur.fetchall()

    def messages(self, match_id: UUID, after: int | None, before: int | None, limit: int):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            if after is not None:
                self.execute(cur, "SELECT * FROM matching_api.messages WHERE match_id=%s AND id>%s ORDER BY id LIMIT %s",
                            (match_id, after, limit))
                return cur.fetchall()
            self.execute(cur, """SELECT * FROM matching_api.messages WHERE match_id=%s
                AND (%s::bigint IS NULL OR id<%s) ORDER BY id DESC LIMIT %s""", (match_id, before, before, limit))
            return list(reversed(cur.fetchall()))

    def send(self, match_id: UUID, actor: UUID, client_id: UUID, text: str):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Serializes messages per conversation so cursors cannot skip an uncommitted message.
            self.execute(cur, "SELECT * FROM matching_api.requests WHERE id=%s FOR UPDATE", (match_id,))
            match = cur.fetchone()
            if not match or actor not in (match["sender_id"], match["recipient_id"]) or match["status"] != "accepted":
                raise HTTPException(403, "El chat requiere un match aceptado.")
            self.execute(cur, """INSERT INTO matching_api.messages(match_id,sender_id,client_message_id,text)
                VALUES (%s,%s,%s,%s) ON CONFLICT(match_id,sender_id,client_message_id) DO NOTHING RETURNING *""",
                        (match_id, actor, client_id, text))
            row = cur.fetchone()
            if row is None:
                self.execute(cur, "SELECT * FROM matching_api.messages WHERE match_id=%s AND sender_id=%s AND client_message_id=%s",
                            (match_id, actor, client_id))
                row = cur.fetchone()
                if row["text"] != text:
                    raise HTTPException(409, "El identificador del mensaje ya fue utilizado.")
            return row
