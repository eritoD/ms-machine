from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import HTTPException


@dataclass(frozen=True)
class Actor:
    id: UUID
    token: str


def authenticate(token: str, settings) -> Actor:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"],
                            issuer=settings.issuer, audience=settings.audience,
                            options={"require": ["sub", "iss", "aud", "iat", "exp", "token_type"]})
        if claims["token_type"] != "access":
            raise ValueError("Wrong token type")
        return Actor(UUID(claims["sub"]), token)
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError) as error:
        raise HTTPException(401, "Tu sesión expiró. Vuelve a iniciar sesión.") from error
