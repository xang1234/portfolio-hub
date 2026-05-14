"""FastAPI app factory and routes.

Slice 1 surface:
  - GET /healthz : JSON connection state (text/html fragment when HX-Request)
  - GET /        : index page with sticky badge that auto-refreshes every 5s

The create_app() factory accepts an injected Broker for testability. Production
entry (``app`` module-level binding) builds the IbkrAdapter from env vars and
manages its lifecycle via FastAPI lifespan.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.broker import Broker


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _is_htmx_request(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def create_app(*, broker: Broker | None = None) -> FastAPI:
    """Build a FastAPI app with the given broker injected.

    When ``broker`` is None, the app constructs an IbkrAdapter from env vars and
    connects on startup via lifespan. Tests pass a fake adapter and skip lifespan.
    """

    if broker is None:
        from app.adapters.ibkr import IbkrAdapter

        broker = IbkrAdapter(
            host=os.environ.get("IB_HOST", "ib-gateway"),
            port=int(os.environ.get("IB_PORT", "4001")),
            client_id=int(os.environ.get("IB_CLIENT_ID", "1")),
        )
        manage_lifecycle = True
    else:
        manage_lifecycle = False

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if manage_lifecycle:
            try:
                await broker.connect()
            except Exception:
                # Slice 1: tolerate startup failure. Reconnection comes in slice 9.
                pass
        yield
        if manage_lifecycle:
            try:
                await broker.disconnect()
            except Exception:
                pass

    app = FastAPI(lifespan=lifespan, title="portfolio-hub")
    app.state.broker = broker
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/healthz")
    async def healthz(request: Request):
        connected = await request.app.state.broker.is_connected()
        if _is_htmx_request(request):
            return templates.TemplateResponse(
                request=request,
                name="partials/status_badge.html",
                context={"connected": connected},
            )
        state = "connected" if connected else "disconnected"
        return JSONResponse({"ibkr": state})

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        connected = await request.app.state.broker.is_connected()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"connected": connected},
        )

    return app


app = create_app()
