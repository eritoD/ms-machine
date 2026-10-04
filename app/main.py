from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.v1.matching import router
from app.core.config import get_settings
from app.database.session import create_pool
from app.repositories.postgres_matching_repository import PostgresMatchingRepository
from app.services.matching_service import MatchingService
from app.services.users_client import UsersClient


def create_app(service=None, settings=None):
    @asynccontextmanager
    async def lifespan(app):
        if service is not None:
            yield
            return
        config = get_settings()
        pool = create_pool(config.database_url)
        try:
            repository = PostgresMatchingRepository(pool)
            repository.migrate()
            with httpx.Client(base_url=config.users_url, timeout=10, trust_env=False) as client:
                app.state.settings = config
                app.state.service = MatchingService(repository, UsersClient(client))
                yield
        finally:
            pool.close()

    app = FastAPI(title="SportMatch Matching", lifespan=lifespan)
    if service is not None:
        app.state.service = service
        app.state.settings = settings
    app.include_router(router)
    return app


app = create_app()
