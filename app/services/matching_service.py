from fastapi import HTTPException

import logging
from datetime import timezone
from uuid import uuid4

from app.schemas.chats import ChatLastMessage, ChatParticipant, ChatSummary, ReadResult
from app.schemas.matching import Connection, MatchingState, Message
from app.services.attachments import check_attachment
from app.services.realtime import NullRealtime

log = logging.getLogger(__name__)

_LABELS = {"IMAGE": "Imagen", "FILE": "Archivo", "LOCATION": "Ubicación"}


class MatchingService:
    def __init__(self, repository, users, attachment_hosts=(), realtime=None):
        self.repository = repository
        self.users = users
        self.attachment_hosts = attachment_hosts
        self.realtime = realtime or NullRealtime()

    @staticmethod
    def peer(row, actor):
        return row["recipient_id"] if row["sender_id"] == actor.id else row["sender_id"]

    def connection(self, row, actor, card=None):
        return Connection(**row, athlete=card or self.users.card(self.peer(row, actor), actor.token))

    def request(self, actor, recipient):
        if actor.id == recipient:
            raise HTTPException(422, "No puedes enviarte una solicitud.")
        card = self.users.card(recipient, actor.token)
        return self.connection(self.repository.request(actor.id, recipient), actor, card)

    def decide(self, actor, request_id, action):
        row = self.repository.get(request_id, actor.id)
        card = self.users.card(self.peer(row, actor), actor.token)
        return self.connection(self.repository.decide(request_id, actor.id,
                               "accepted" if action == "accept" else "rejected"), actor, card)

    def cancel(self, actor, request_id):
        self.repository.cancel(request_id, actor.id)

    def state(self, actor):
        rows = self.repository.state(actor.id)
        peers = list(dict.fromkeys(self.peer(row, actor) for row in rows if row["status"] != "rejected"))
        cards = self.users.cards(peers, actor.token)
        result = MatchingState(incoming=[], outgoing=[], matches=[], hidden_user_ids=[])
        for row in rows:
            peer = self.peer(row, actor)
            if row["status"] == "rejected":
                result.hidden_user_ids.append(peer)
                continue
            if peer not in cards:
                continue  # Deactivated accounts cannot be contacted.
            connection = self.connection(row, actor, cards[peer])
            if row["status"] == "accepted":
                result.matches.append(connection)
            elif row["recipient_id"] == actor.id:
                result.incoming.append(connection)
            else:
                result.outgoing.append(connection)
        return result

    def match(self, actor, match_id):
        row = self.repository.get(match_id, actor.id)
        if row["status"] != "accepted":
            raise HTTPException(403, "El chat requiere un match aceptado.")
        return self.connection(row, actor)

    @staticmethod
    def message(row):
        return Message(**{**row, "id": str(row["id"]), "is_read": row.get("read_at") is not None})

    def messages(self, actor, match_id, after, before, limit):
        self.match(actor, match_id)
        if after is not None and before is not None:
            raise HTTPException(422, "Usa un solo cursor de mensajes.")
        # Reading the conversation marks the other person's messages as read.
        return [self.message(row) for row in self.repository.messages(match_id, after, before, limit, reader=actor.id)]

    def chats(self, actor, limit, offset):
        rows = self.repository.chats(actor.id, limit, offset)
        cards = self.users.cards(list(dict.fromkeys(self.peer(row, actor) for row in rows)), actor.token)
        chats = []
        for row in rows:
            peer = self.peer(row, actor)
            card = cards.get(peer)
            if card is None:
                continue  # Deactivated accounts cannot be contacted.
            last = None
            if row["last_content"] is not None:
                last = ChatLastMessage(content=row["last_content"], senderId=row["last_sender_id"], type=row["last_type"],
                                       createdAt=row["last_message_at"].astimezone(timezone.utc))
            chats.append(ChatSummary(
                conversationId=row["id"], lastMessage=last, unreadCount=row["unread_count"],
                participant=ChatParticipant(userId=peer, avatarUrl=card.get("foto_perfil"),
                                            name=f"{card['nombre']} {card['apellido_inicial']}".strip())))
        return chats

    def mark_read(self, actor, match_id):
        self.match(actor, match_id)  # Only a participant of an accepted match.
        return ReadResult(conversationId=match_id, markedAsRead=self.repository.mark_read(match_id, actor.id),
                          unreadCount=0)

    def send(self, actor, match_id, payload):
        connection = self.match(actor, match_id)
        row = self.repository.send(match_id, actor.id, payload.client_message_id, payload.text)
        return self._saved(connection, actor, row)

    def _saved(self, connection, actor, row):
        """The message as stored; a new one is also announced to the people who are connected."""
        row = dict(row)
        created = row.pop("_created", True)
        message = self.message(row)
        if created:
            self._announce(connection, actor, message)
        return message

    def _announce(self, connection, actor, message):
        """Real-time events for a message that is already saved. Best effort: whatever fails here
        (a socket, a count) is logged and never reaches the person who sent the message."""
        try:
            conversation = str(message.match_id)
            recipient = connection.recipient_id if connection.sender_id == actor.id else connection.sender_id
            self.realtime.emit_to_conversation(message.match_id, (connection.sender_id, connection.recipient_id),
                                               {"type": "new_message", "conversationId": conversation,
                                                "message": message.model_dump(mode="json")})
            if self.realtime.is_connected(recipient):
                unread, total = self.repository.unread_counts(recipient, message.match_id)
                content = message.text if message.message_type == "TEXT" or message.text.strip() \
                    else _LABELS[message.message_type]
                self.realtime.emit_to_user(recipient, {
                    "type": "unread_update", "conversationId": conversation, "unreadCount": unread,
                    "totalUnread": total,
                    "lastMessage": {"content": content, "senderId": str(message.sender_id),
                                    "createdAt": message.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                                    "type": message.message_type}}, skip_conversation=message.match_id)
        except Exception:
            log.exception("Could not announce a saved message in real time")

    def send_message(self, actor, match_id, spec):
        """Send a TEXT, IMAGE, FILE or LOCATION message (HU-50). Being in the conversation is checked first."""
        connection = self.match(actor, match_id)
        fields = {"text": spec.content or "", "message_type": spec.type, "location_name": spec.location_name}
        if spec.type in ("IMAGE", "FILE"):
            name = check_attachment(spec.type, spec.attachment_url, self.attachment_hosts)
            fields["attachment_url"] = spec.attachment_url
            if spec.type == "FILE":
                fields["attachment_name"] = spec.attachment_name or name[:120]
        elif spec.type == "LOCATION":
            fields["latitude"], fields["longitude"] = spec.latitude, spec.longitude
        return self._saved(connection, actor, self.repository.send(match_id, actor.id, spec.client_message_id or uuid4(), **fields))
