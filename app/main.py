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
from sse_starlette.sse import EventSourceResponse

from app.core.broker import Broker, Position
from app.core.live_positions import LivePositions, stream_events


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _is_htmx_request(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def create_app(
    *,
    broker: Broker | None = None,
    live_positions: LivePositions | None = None,
) -> FastAPI:
    """Build a FastAPI app with the given broker (and optional LivePositions).

    When ``broker`` is None, the app constructs an IbkrAdapter from env vars and
    connects on startup via lifespan. Tests pass a fake adapter and skip lifespan.

    ``live_positions`` is the observable in-memory snapshot the SSE handler
    streams from. If None, a fresh LivePositions is created.
    """

    store = None
    if broker is None:
        manage_lifecycle = True
    else:
        manage_lifecycle = False

    if live_positions is None:
        live_positions = LivePositions()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal broker, store
        if manage_lifecycle:
            from app.adapters.ibkr import IbkrAdapter
            from app.db.store import Store

            data_dir = Path(os.environ.get("DATA_DIR", "./data"))
            data_dir.mkdir(parents=True, exist_ok=True)
            store = Store(data_dir / "portfolio.db")
            try:
                await store.init_schema()
            except Exception:
                pass

            broker = IbkrAdapter(
                host=os.environ.get("IB_HOST", "ib-gateway"),
                port=int(os.environ.get("IB_PORT", "4003")),
                client_id=int(os.environ.get("IB_CLIENT_ID", "1")),
                store=store,
                live_positions=live_positions,
            )
            app.state.broker = broker
            try:
                await broker.connect()
            except Exception:
                # Slice 1: tolerate startup failure. Reconnection comes in slice 9.
                pass
        yield
        if manage_lifecycle and broker is not None:
            try:
                await broker.disconnect()
            except Exception:
                pass
            if store is not None:
                try:
                    await store.close()
                except Exception:
                    pass

    app = FastAPI(lifespan=lifespan, title="portfolio-hub")
    app.state.broker = broker
    app.state.live_positions = live_positions
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Expose symbols helpers so templates can compute display data from
    # canonical fields without polluting the Position dataclass.
    from app.core.symbols import flag_for_exchange

    templates.env.globals["flag_for_exchange"] = flag_for_exchange

    def render_rows(positions: list[Position]) -> str:
        if not positions:
            return ""
        template = templates.get_template("partials/holdings_row.html")
        return "".join(
            template.render({"position": p, "flag_for_exchange": flag_for_exchange})
            for p in positions
        )

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

    @app.get("/stream/holdings")
    async def stream_holdings(request: Request):
        """SSE endpoint streaming row-level deltas of the current portfolio.

        The HTMX SSE extension on the client connects via `sse-connect` and
        listens for named events: 'snapshot' (initial full tbody on connect)
        and 'positions' (delta — only changed rows, with hx-swap-oob).
        """
        live = request.app.state.live_positions
        generator = stream_events(live, render_rows)
        return EventSourceResponse(generator)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        broker = request.app.state.broker
        live = request.app.state.live_positions
        connected = await broker.is_connected()
        # Prefer the live, tick-updated snapshot when it has data; otherwise
        # fall back to a direct broker.get_positions() fetch (covers the slice
        # 1/2 test path where live_positions is never seeded).
        positions = live.get_all()
        if not positions and connected:
            try:
                positions = await broker.get_positions()
            except NotImplementedError:
                positions = []
        positions.sort(key=lambda p: p.market_value_native, reverse=True)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"connected": connected, "positions": positions},
        )

    return app


app = create_app()
