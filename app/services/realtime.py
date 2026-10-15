"""Real-time delivery of chat events over WebSockets (HU-52).

One process-wide hub keeps the open sockets. The REST routes that save a message are synchronous
and run in a thread pool, while the sockets live on the event loop, so everything the routes call
here (`emit_to_conversation`, `emit_to_user`, `is_connected`, `is_active`) is thread-safe and never
blocks: it only hands the event to the loop. A failure here must never affect saving a message, so
callers wrap it and the hub itself swallows delivery errors.

Limits of this first version: the sockets of one process only (no Redis or PostgreSQL LISTEN/NOTIFY
between replicas yet), so it assumes a single instance of the service.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from uuid import UUID

log = logging.getLogger(__name__)

MAX_CONNECTIONS_PER_USER = 5
QUEUE_SIZE = 100


@dataclass(eq=False)
class Connection:
    websocket: object
    user_id: UUID
    # The conversation this socket has open ("focused"); at most one per socket.
    joined: UUID | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE))
    closed: bool = False


def _uuid(value):
    """Conversation ids reach the hub as UUIDs from the services and as text from sockets."""
    return value if value is None or isinstance(value, UUID) else UUID(str(value))


class NullRealtime:
    """No sockets: used when the service runs without real time (and in older tests)."""

    def emit_to_conversation(self, conversation_id, participants, event): ...

    def emit_to_user(self, user_id, event, skip_conversation=None): ...

    def is_connected(self, user_id) -> bool:
        return False

    def is_active(self, user_id, conversation_id) -> bool:
        return False


class RealtimeHub(NullRealtime):
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._by_user: dict[UUID, list[Connection]] = {}
        self._tasks: set[asyncio.Task] = set()

    def start(self):
        """Bind to the running event loop (called when the application starts)."""
        self._loop = asyncio.get_running_loop()

    # -- thread-safe API used by the routes ------------------------------------------------------

    def is_connected(self, user_id) -> bool:
        return bool(self._by_user.get(user_id))

    def is_active(self, user_id, conversation_id) -> bool:
        """Whether the person has this conversation open right now on some device. A future push
        sender asks this to stay quiet for someone who is looking at the chat."""
        conversation_id = _uuid(conversation_id)
        return any(c.joined == conversation_id for c in self._by_user.get(user_id, ()))

    def emit_to_conversation(self, conversation_id, participants, event):
        """Send the event to the sockets of the two participants that have the conversation open."""
        conversation_id = _uuid(conversation_id)
        self._schedule(lambda: self._fan_out(
            [c for user in participants for c in self._by_user.get(user, ()) if c.joined == conversation_id], event))

    def emit_to_user(self, user_id, event, skip_conversation=None):
        """Send the event to every socket of one person, except those with `skip_conversation` open
        (they already receive that conversation's own events)."""
        skip_conversation = _uuid(skip_conversation)
        self._schedule(lambda: self._fan_out(
            [c for c in self._by_user.get(user_id, ()) if skip_conversation is None or c.joined != skip_conversation],
            event))

    def _schedule(self, work):
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(work)
        except RuntimeError:  # the loop closed in between
            log.debug("Realtime event dropped: the event loop is closed")

    # -- event-loop side ----------------------------------------------------------------------------

    def register(self, connection: Connection) -> list[Connection]:
        """Add a socket; returns the oldest ones to close when the person has too many."""
        connections = self._by_user.setdefault(connection.user_id, [])
        connections.append(connection)
        return [connections.pop(0) for _ in range(max(len(connections) - MAX_CONNECTIONS_PER_USER, 0))]

    def unregister(self, connection: Connection):
        connections = self._by_user.get(connection.user_id, [])
        if connection in connections:
            connections.remove(connection)
        if not connections:
            self._by_user.pop(connection.user_id, None)

    def enqueue(self, connection: Connection, event) -> bool:
        """Queue a frame for one socket; a socket that cannot keep up is dropped, not waited for."""
        if connection.closed:
            return False
        try:
            connection.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            log.warning("Realtime queue full for a socket; closing it")
            connection.closed = True
            self._spawn(self._close(connection, 1013))
            return False

    def _fan_out(self, connections, event):
        for connection in connections:
            self.enqueue(connection, event)

    async def _close(self, connection: Connection, code: int):
        try:
            await connection.websocket.close(code)
        except Exception:  # already closed
            pass

    def _spawn(self, coroutine):
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def close_evicted(self, evicted: list[Connection]):
        for connection in evicted:
            connection.closed = True
            self._spawn(self._close(connection, 4409))
