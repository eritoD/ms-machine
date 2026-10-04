import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    users_url: str
    issuer: str
    audience: str


def get_settings() -> Settings:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret.encode()) < 32:
        raise RuntimeError("JWT_SECRET must contain at least 32 bytes")
    return Settings(
        database_url=os.environ["MATCHING_DATABASE_URL"], jwt_secret=secret,
        users_url=os.environ.get("USERS_SERVICE_URL", "http://ms_users:8001"),
        issuer=os.environ.get("JWT_ISSUER", "sportmatch-auth"),
        audience=os.environ.get("JWT_AUDIENCE", "sportmatch-mobile"),
    )
