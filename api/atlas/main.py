"""The FastAPI application:  uvicorn atlas.main:app --reload

The web process serves requests and enqueues work. It does not run jobs — that
is `python -m atlas.jobs.worker`, in its own process, so a burst of ingestion
cannot starve the request path and a deploy of one does not restart the other.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from atlas import __version__
from atlas.config import settings
from atlas.db import close_pool, pool
from atlas.ingest import ExtractionError
from atlas.llm import ProviderError
from atlas.routers import auth, chat, documents, health, search, sources

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("atlas")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Open the pool at startup rather than on the first request, so a bad
    # DATABASE_URL fails the deploy instead of the first user's query.
    try:
        await pool()
        log.info("atlas %s ready", __version__)
    except Exception:  # noqa: BLE001 — /health must still answer and explain
        log.exception("could not open the database pool; /health will report why")
    yield
    await close_pool()


app = FastAPI(
    title="Atlas",
    version=__version__,
    description=(
        "Permission-aware hybrid retrieval over a multi-source corpus. "
        "Dense + BM25, fused by weighted RRF, with the caller's group keys "
        "applied inside the retrieval scan rather than after it."
    ),
    lifespan=lifespan,
)

# The frontend is a separate deployable on a separate origin, so this is
# configuration (ATLAS_CORS_ORIGINS) rather than a constant. Credentials are
# allowed because the session is a cookie, and the CORS spec rejects "*"
# alongside credentials — an explicit list is the only thing that works.
#
# Read defensively: a broken .env must not cost us CORS too, or the frontend
# cannot even reach /health to find out what is wrong.
try:
    _origins = settings().allowed_origins
except Exception:  # noqa: BLE001 — /health is the diagnostic; keep it reachable
    _origins = ["http://localhost:3000", "http://localhost:3400"]
    log.exception("could not read ATLAS_CORS_ORIGINS; falling back to localhost")

async def _unhandled(request: Request, call_next: Any) -> Any:
    """Turns an unhandled exception into a JSON 500 the browser can actually see.

    Starlette's own 500 is produced by ServerErrorMiddleware, which sits
    *outside* the CORS layer — so the response carries no
    access-control-allow-origin, the browser blocks it, and `fetch` rejects with
    a bare network error. The frontend then reports "cannot reach the API" for a
    server that is up and answering. Catching here, inside CORS, keeps the
    headers on and lets the real status through.
    """
    try:
        return await call_next(request)
    except Exception:  # noqa: BLE001 — the point is that nothing escapes
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "The server hit an unexpected error. Check the API logs.",
                "kind": "server",
            },
        )


# Order matters and is the reverse of the calls: add_middleware prepends, so the
# last one added is outermost. CORS must be outermost, which means it is added
# last — otherwise the handler above returns a response CORS never annotates.
app.add_middleware(BaseHTTPMiddleware, dispatch=_unhandled)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ExtractionError)
async def _extraction_error(_: Request, exc: ExtractionError) -> JSONResponse:
    # The document is the problem, not the request shape — 400 with the parser's
    # own message, which is written to be read by a person.
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc), "kind": "extraction"},
    )


@app.exception_handler(ProviderError)
async def _provider_error(_: Request, exc: ProviderError) -> JSONResponse:
    # A missing key is the operator's problem (503, actionable); anything else
    # from a provider is upstream (502).
    unconfigured = not exc.retryable and "is not set" in str(exc)
    return JSONResponse(
        status_code=(
            status.HTTP_503_SERVICE_UNAVAILABLE
            if unconfigured
            else status.HTTP_502_BAD_GATEWAY
        ),
        content={"detail": str(exc), "kind": "provider", "provider": exc.provider},
    )


app.include_router(auth.router)
app.include_router(health.router)
app.include_router(documents.router)
app.include_router(sources.router)
app.include_router(search.router)
app.include_router(chat.router)


def run() -> None:
    """Console entrypoint: `uv run atlas-api`.

    PORT rather than a flag, because every managed host (Railway, Render, Fly,
    Cloud Run) injects it and a hardcoded port makes the container unroutable.
    Reload is off here — that is a development choice, made explicitly with
    `uv run uvicorn atlas.main:app --reload`.
    """
    import os

    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),  # noqa: S104 — containers bind all
        port=int(os.getenv("PORT", "8000")),
    )


@app.get("/")
async def root() -> dict[str, Any]:
    cfg = settings()
    return {
        "name": "atlas",
        "version": __version__,
        "generation_model": cfg.generation_model,
        "embedding_model": cfg.embedding_model,
        "docs": "/docs",
    }
