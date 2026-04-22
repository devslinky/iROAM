"""FastAPI application factory.

``app`` is the module-level ASGI callable expected by ``uvicorn`` in both
docker-compose and ``make api``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from apps.api.routers import feed_status, health, iroam, replay, routes, trajectories, vehicles
from core.config import get_settings
from core.logging import configure_logging, get_logger

_logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    configure_logging()
    _logger.info("api_start")
    yield
    _logger.info("api_stop")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="TTC GTFS-Realtime Platform",
        description=(
            "Append-only ingestion, storage, and query API for the TTC "
            "GTFS-Realtime VehiclePositions feed."
        ),
        version="0.2.0",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        _logger.exception("unhandled_exception", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error"},
        )

    app.include_router(health.router)
    app.include_router(feed_status.router)
    app.include_router(vehicles.router)
    app.include_router(routes.router)
    app.include_router(replay.router)
    app.include_router(trajectories.router)
    app.include_router(iroam.router)

    # Serve the iROAM dashboard as static files. Mount last so /iroam API
    # routes (registered above) win over the static mount when paths overlap.
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=static_dir, html=True), name="iroam-ui")

    return app


app = create_app()
