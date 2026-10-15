import os
from dataclasses import dataclass

from app.services.attachments import parse_hosts


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    users_url: str
    issuer: str
    audience: str
    # Hosts whose https links the chat accepts as image/file attachments (empty: attachments off).
    attachment_hosts: tuple[str, ...] = ()


def get_settings() -> Settings:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret.encode()) < 32:
        raise RuntimeError("JWT_SECRET must contain at least 32 bytes")
    return Settings(
        database_url=os.environ["MATCHING_DATABASE_URL"], jwt_secret=secret,
        users_url=os.environ.get("USERS_SERVICE_URL", "http://ms_users:8001"),
        issuer=os.environ.get("JWT_ISSUER", "sportmatch-auth"),
        audience=os.environ.get("JWT_AUDIENCE", "sportmatch-mobile"),
        attachment_hosts=parse_hosts(os.environ.get("CHAT_ATTACHMENT_HOSTS", "")),
    )
