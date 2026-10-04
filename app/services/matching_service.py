from fastapi import HTTPException

from app.schemas.matching import Connection, MatchingState, Message


class MatchingService:
    def __init__(self, repository, users):
        self.repository = repository
        self.users = users

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
        return Message(**{**row, "id": str(row["id"])})

    def messages(self, actor, match_id, after, before, limit):
        self.match(actor, match_id)
        if after is not None and before is not None:
            raise HTTPException(422, "Usa un solo cursor de mensajes.")
        return [self.message(row) for row in self.repository.messages(match_id, after, before, limit)]

    def send(self, actor, match_id, payload):
        self.match(actor, match_id)
        return self.message(self.repository.send(match_id, actor.id, payload.client_message_id, payload.text))
