from uuid import UUID

import httpx
from fastapi import HTTPException


class UsersClient:
    """Only HTTP access to public athlete cards; never opens the Users database."""
    def __init__(self, client: httpx.Client, internal_token: str | None = None):
        self.client = client
        self.internal_token = internal_token

    def _call(self, method, path, token, **kwargs):
        try:
            response = self.client.request(method, path,
                headers={"Authorization": f"Bearer {token}"}, **kwargs)
        except httpx.RequestError as error:
            raise HTTPException(503, "No se pudo validar el deportista. Inténtalo de nuevo.") from error
        if response.status_code in (401, 403, 404):
            raise HTTPException(response.status_code, "Deportista no disponible o sesión sin autorización.")
        if response.status_code != 200:
            raise HTTPException(503, "El servicio de usuarios no está disponible.")
        return response.json()

    def contactable(self, viewer_id: UUID, user_ids: list[UUID]) -> set[UUID] | None:
        """Which of these people `viewer_id` can still talk to (active, not blocked either way, not suspended), asked to
        Ms_Users on the internal network. None = not known (no secret, or Ms_Users did not answer)."""
        if not self.internal_token:
            return None
        found: set[UUID] = set()
        try:
            for start in range(0, len(user_ids), 100):
                response = self.client.post(f"/internal/v1/accounts/{viewer_id}/contactable",
                                            headers={"X-Internal-Service-Token": self.internal_token},
                                            json={"ids": [str(uid) for uid in user_ids[start:start + 100]]})
                if response.status_code != 200:
                    return None
                found.update(UUID(value) for value in response.json()["ids"])
        except (httpx.RequestError, ValueError, KeyError, TypeError):
            return None
        return found

    def card(self, user_id: UUID, token: str):
        return self._call("GET", f"/api/v1/users/athletes/{user_id}", token)

    def cards(self, user_ids: list[UUID], token: str):
        cards = {}
        for start in range(0, len(user_ids), 100):
            batch = self._call("POST", "/api/v1/users/athletes/cards", token,
                               json=[str(uid) for uid in user_ids[start:start + 100]])
            cards.update({UUID(card["user_id"]): card for card in batch})
        return cards
