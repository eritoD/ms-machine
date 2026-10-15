import math
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1.chats import router as chats_router
from app.api.v1.matching import router
from app.api.v1.realtime import router as realtime_router
from app.core.config import get_settings
from app.database.session import create_pool
from app.repositories.postgres_matching_repository import PostgresMatchingRepository
from app.services.matching_service import MatchingService
from app.services.realtime import RealtimeHub
from app.services.users_client import UsersClient


def _finite_or_text(value: float):
    """A number such as 1e999 reaches validation as infinity, which JSON cannot carry: the error
    message that echoes the received value would fail to serialize and answer 500 instead of 422."""
    return value if math.isfinite(value) else str(value)


def _start_realtime(app):
    """Bind the WebSocket hub of the service to the running event loop."""
    app.state.hub = getattr(app.state.service, "realtime", None) or RealtimeHub()
    if hasattr(app.state.hub, "start"):
        app.state.hub.start()


def create_app(service=None, settings=None):
    @asynccontextmanager
    async def lifespan(app):
        if service is not None:
            _start_realtime(app)
            yield
            return
        config = get_settings()
        pool = create_pool(config.database_url)
        try:
            repository = PostgresMatchingRepository(pool)
            repository.migrate()
            with httpx.Client(base_url=config.users_url, timeout=10, trust_env=False) as client:
                app.state.settings = config
                hub = RealtimeHub()
                app.state.service = MatchingService(repository, UsersClient(client), config.attachment_hosts, realtime=hub)
                _start_realtime(app)
                yield
        finally:
            pool.close()

    app = FastAPI(title="SportMatch Matching", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, error: RequestValidationError) -> JSONResponse:
        # Same 422 body as FastAPI's own, but able to carry an infinite number.
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(
            error.errors(), custom_encoder={float: _finite_or_text})})
    if service is not None:
        app.state.service = service
        app.state.settings = settings
    app.include_router(router)
    app.include_router(chats_router)
    app.include_router(realtime_router)
    return app


app = create_app()
