"""FastAPI web application factory, middleware, and core endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from solution.config import get_db_path
from solution.db import get_db_connection, init_db
from solution.web.config import (
    ALLOWED_WORKFLOWS,
    DEFAULT_REPORTS_DIR,
    DEFAULT_UPLOADS_DIR,
    FileSizeLimitExceeded,
)
from solution.web.routes.dashboard import router as dashboard_router
from solution.web.routes.reports import router as reports_router
from solution.web.routes.upload import router as upload_router


def create_app(
    db_path: Path | str | None = None,
    reports_dir: Path | None = None,
    uploads_dir: Path | None = None,
) -> FastAPI:
    """Create and configure the FastAPI web application."""
    app = FastAPI(
        title="Interaction Ledger & Reporting Web Application",
        description="Local web-based data upload, ingestion, and reporting platform for Agentic Universe.",
        version="1.0.0",
    )

    # State initialization
    app.state.db_path = Path(db_path) if db_path else get_db_path()
    app.state.reports_dir = Path(reports_dir) if reports_dir else DEFAULT_REPORTS_DIR
    app.state.uploads_dir = Path(uploads_dir) if uploads_dir else DEFAULT_UPLOADS_DIR

    # Ensure required runtime directories exist
    app.state.reports_dir.mkdir(parents=True, exist_ok=True)
    app.state.uploads_dir.mkdir(parents=True, exist_ok=True)

    # Restrict CORS to local development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost",
            "http://127.0.0.1",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # Global Exception Handlers
    @app.exception_handler(FileSizeLimitExceeded)
    async def file_size_exception_handler(request: Request, exc: FileSizeLimitExceeded):
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"error": "File size limit exceeded", "message": str(exc)},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Bad request", "message": str(exc)},
        )

    # Core Health Check Endpoint
    @app.get("/api/health", tags=["System"])
    async def health_check() -> Dict[str, Any]:
        """Verify server status, database connectivity, and environment health."""
        db_ok = False
        leads_count = 0
        events_count = 0

        try:
            with get_db_connection(app.state.db_path) as conn:
                init_db(conn)
                # Verify basic table reads
                row_leads = conn.execute("SELECT COUNT(*) as c FROM leads").fetchone()
                row_events = conn.execute("SELECT COUNT(*) as c FROM interaction_ledger").fetchone()
                leads_count = row_leads["c"] if row_leads else 0
                events_count = row_events["c"] if row_events else 0
                db_ok = True
        except Exception as e:
            db_ok = False

        return {
            "status": "healthy" if db_ok else "degraded",
            "database": {
                "connected": db_ok,
                "leads_count": leads_count,
                "events_count": events_count,
            },
            "workflows": list(ALLOWED_WORKFLOWS),
            "version": "1.0.0",
        }

    # Register Routers
    app.include_router(upload_router, prefix="/api/upload", tags=["Upload"])
    app.include_router(dashboard_router, prefix="/api/dashboard", tags=["Dashboard"])
    app.include_router(reports_router, prefix="/api/reports", tags=["Reports"])

    # Root route serving frontend SPA
    static_index = Path(__file__).resolve().parent / "static" / "index.html"

    @app.get("/", response_class=HTMLResponse, tags=["Frontend"])
    async def get_index():
        if static_index.exists():
            return HTMLResponse(content=static_index.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>Dashboard loading...</h1>")

    return app


# Default app instance for Uvicorn runner
app = create_app()
