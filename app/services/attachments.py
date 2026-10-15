"""Which attachment URLs the chat accepts (HU-50).

This service stores no files: an attachment is a link to a file the app already uploaded
to a storage the project trusts. Accepting any URL would let a user make the other
person's app load content from a server of their choosing (which reveals that person's IP
address), so only https links to configured hosts, with the right file extension, pass.
"""
import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
FILE_EXTENSIONS = frozenset({".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv"})
ALLOWED = {"IMAGE": IMAGE_EXTENSIONS, "FILE": FILE_EXTENSIONS}
_HOST = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def parse_hosts(raw: str) -> tuple[str, ...]:
    """CHAT_ATTACHMENT_HOSTS: comma separated host names; '*.example.com' also allows subdomains."""
    hosts = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    for host in hosts:
        if not _HOST.match(host):
            raise RuntimeError(f"CHAT_ATTACHMENT_HOSTS has an invalid host: {host!r}")
    return hosts


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    return any(host == item or (item.startswith("*.") and host.endswith(item[1:])) for item in allowed)


def check_attachment(kind: str, url: str, allowed_hosts: tuple[str, ...]) -> str:
    """Return the file name in the link, or raise 422 (503 when attachments are not configured)."""
    if not allowed_hosts:
        raise HTTPException(503, "Los adjuntos no están habilitados en este momento.")
    try:
        parts = urlsplit(url)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        raise HTTPException(422, "La URL del adjunto no es válida.") from None
    if parts.scheme != "https" or not host or parts.username or parts.password or port not in (None, 443):
        raise HTTPException(422, "El adjunto debe ser un enlace https sin usuario ni puerto.")
    if not _host_allowed(host, allowed_hosts):
        raise HTTPException(422, "El adjunto debe estar en el almacenamiento de SportMatch.")
    decoded = unquote(parts.path)
    path = PurePosixPath(decoded)
    # A path ending in '/' is a directory: PurePosixPath would ignore the slash and read an extension.
    if decoded.endswith("/") or path.suffix.lower() not in ALLOWED[kind]:
        extensions = ", ".join(sorted(extension[1:] for extension in ALLOWED[kind]))
        raise HTTPException(422, f"Formato no permitido. Formatos de {'imagen' if kind == 'IMAGE' else 'archivo'}: {extensions}.")
    return path.name
