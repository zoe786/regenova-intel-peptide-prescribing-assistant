"""REGENOVA-Intel FastAPI application entry point.

Sets up the FastAPI app with lifespan management, middleware,
routers, and global error handling.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from apps.api.config import get_settings
from apps.api.routers import chat, health, ingest

logger = logging.getLogger(__name__)

_APP_TITLE = "REGENOVA-Intel API"
_APP_DESCRIPTION = (
    "Evidence-tiered AI assistant for peptide prescribing decisions. "
    "Provides retrieval-augmented generation with safety rules, citation integrity, "
    "and evidence-tier weighting. **Clinical decision support only** — not medical advice."
)
_APP_VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown lifecycle.

    Startup: log configuration, warm up services.
    Shutdown: clean up resources.
    """
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    logger.info(
        "🧬 REGENOVA-Intel API starting: version=%s env=%s model=%s",
        _APP_VERSION,
        settings.environment,
        settings.llm_model,
    )
    logger.info(
        "Vector backend=%s chroma_dir=%s",
        settings.vector_db_backend,
        settings.chroma_persist_dir,
    )

    yield  # Application runs here

    logger.info("REGENOVA-Intel API shutting down")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """
    settings = get_settings()

    app = FastAPI(
        title=_APP_TITLE,
        description=_APP_DESCRIPTION,
        version=_APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # ── CORS Middleware ────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Disclaimer Middleware ──────────────────────────────────────────────
    @app.middleware("http")
    async def add_decision_support_header(
        request: Request, call_next: object
    ) -> Response:
        """Add X-Decision-Support-Only header to every response."""
        response: Response = await call_next(request)  # type: ignore[operator]
        response.headers["X-Decision-Support-Only"] = "true"
        response.headers["X-Request-ID"] = str(uuid.uuid4())
        return response

    # ── Request Timing Middleware ──────────────────────────────────────────
    @app.middleware("http")
    async def add_timing_header(request: Request, call_next: object) -> Response:
        """Add X-Process-Time-Ms header for observability."""
        start = time.time()
        response: Response = await call_next(request)  # type: ignore[operator]
        elapsed_ms = int((time.time() - start) * 1000)
        response.headers["X-Process-Time-Ms"] = str(elapsed_ms)
        return response

    # ── Global Exception Handler ───────────────────────────────────────────
    @app.exception_handler(Exception)
    async def global_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Return RFC 7807 Problem Detail for unhandled exceptions."""
        logger.error(
            "Unhandled exception for %s %s: %s",
            request.method,
            request.url.path,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "type": "https://regenova-intel.example.com/errors/internal",
                "title": "Internal Server Error",
                "status": 500,
                "detail": "An unexpected error occurred. Check server logs.",
                "instance": str(request.url),
            },
        )

    # ── Routers ───────────────────────────────────────────────────────────
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(ingest.router)

    return app


# Application instance used by uvicorn
app = create_app()
