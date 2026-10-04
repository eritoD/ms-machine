from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import authenticate

bearer = HTTPBearer(auto_error=False)


def get_service(request: Request):
    return request.app.state.service


def get_actor(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Inicia sesión para hacer match.")
    actor = authenticate(credentials.credentials, request.app.state.settings)
    # Users checks current status/role/verification on every request, even with an old JWT.
    request.app.state.service.users.card(actor.id, actor.token)
    return actor
