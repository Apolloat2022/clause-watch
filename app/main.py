"""FastAPI application factory.

Dependencies (repositories, blob store, queue, analyzer) are assembled once in
the lifespan and hung off `app.state.deps` — never constructed at import time,
so importing this module performs no network or credential work.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.contracts import router as contracts_router
from app.api.health import router as health_router
from app.api.obligations import router as obligations_router
from app.auth import EntraIdAuthMiddleware
from app.config import settings
from app.deps import Dependencies, build_dependencies
from app.ingest.doc_intelligence import LayoutAnalyzer

logger = logging.getLogger(__name__)


def create_app(
    *,
    dependencies: Dependencies | None = None,
    analyzer: LayoutAnalyzer | None = None,
) -> FastAPI:
    """`dependencies` is the test seam: pass a fully-built container to run the
    real routes against local adapters. `analyzer` is the narrower seam for
    supplying a layout source while keeping everything else default."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=settings.log_level)

        if not (settings.entra_tenant_id and settings.entra_audience):
            logger.warning(
                "Entra ID auth is not configured - every endpoint except /healthz is "
                "unauthenticated. Expected for local runs; never for a deployed revision."
            )

        owns_deps = dependencies is None
        app.state.deps = dependencies or build_dependencies(settings, analyzer=analyzer)
        logger.info("startup complete", extra={"environment": settings.environment})
        try:
            yield
        finally:
            # Only close what this lifespan built. A caller-supplied container
            # (tests, the smoke script) belongs to the caller, and closing it
            # here would tear down adapters they may still be asserting on.
            if owns_deps:
                await app.state.deps.aclose()
            logger.info("shutdown complete")

    app = FastAPI(title="ClauseWatch", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(contracts_router)
    app.include_router(obligations_router)

    # Added only when configured, so an unset tenant leaves the app genuinely
    # unwrapped rather than running a middleware that waves everything through —
    # a permanently-disabled guard is the kind of thing that stays disabled by
    # accident. The lifespan above warns loudly when this branch is skipped.
    if settings.entra_tenant_id and settings.entra_audience:
        app.add_middleware(
            EntraIdAuthMiddleware,
            tenant_id=settings.entra_tenant_id,
            audience=settings.entra_audience,
        )

    return app


app = create_app()
