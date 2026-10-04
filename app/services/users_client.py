from uuid import UUID

import httpx
from fastapi import HTTPException


class UsersClient:
    """Only HTTP access to public athlete cards; never opens the Users database."""
    def __init__(self, client: httpx.Client):
        self.client = client

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

    def card(self, user_id: UUID, token: str):
        return self._call("GET", f"/api/v1/users/athletes/{user_id}", token)

    def cards(self, user_ids: list[UUID], token: str):
        cards = {}
        for start in range(0, len(user_ids), 100):
            batch = self._call("POST", "/api/v1/users/athletes/cards", token,
                               json=[str(uid) for uid in user_ids[start:start + 100]])
            cards.update({UUID(card["user_id"]): card for card in batch})
        return cards
