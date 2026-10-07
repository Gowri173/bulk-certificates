"""FastAPI application initialization, lifespan lifecycle hooks, and router registration."""

from contextlib import asynccontextmanager
import logging
from typing import AsyncGenerator
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.certificates import router as certificates_router
from app.api.jobs import router as jobs_router
from app.config import get_settings
from app.database import init_db
from app.worker.queue import recover_incomplete_jobs, shutdown


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown events."""
    settings = get_settings()

    # Configure logging format and level
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("app.main")
    logger.info("Initializing Bulk Certificate Generator (MODE=%s)...", settings.PROCESSING_MODE)

    # 1. Initialize database schema
    init_db()

    # 2. Recover incomplete jobs from previous runs
    recover_incomplete_jobs()

    yield

    # Clean shutdown of worker threads
    logger.info("Shutting down worker threads...")
    shutdown()


app = FastAPI(
    title="Bulk Certificate Generator API",
    description="Backend API for generating PDF certificates from bulk requests with async processing, failure isolation, and ZIP downloads.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Format Pydantic validation errors with readable detail messages."""
    errors = []
    for err in exc.errors():
        loc = " -> ".join(str(item) for item in err.get("loc", []))
        msg = err.get("msg", "Invalid value")
        errors.append(f"{loc}: {msg}" if loc else msg)

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "; ".join(errors)},
    )


@app.get(
    "/health",
    tags=["Health"],
    summary="Health check",
    description="Simple health check endpoint returning system status.",
)
def health_check() -> dict[str, str]:
    """Return health status."""
    return {"status": "ok"}


# Register API v1 routes
app.include_router(jobs_router, prefix="/api/v1")
app.include_router(certificates_router, prefix="/api/v1")
