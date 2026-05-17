"""FastAPI app factory and routes.

Slice 1 surface:
  - GET /healthz : JSON connection state (text/html fragment when HX-Request)
  - GET /        : index page with sticky badge that auto-refreshes every 5s

The create_app() factory accepts an injected Broker for testability. Production
entry (``app`` module-level binding) builds the IbkrAdapter from env vars and
manages its lifecycle via FastAPI lifespan.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app.core.broker import Broker, ConnectionState, Position
from app.core.live_positions import LivePositions, stream_events
from app.core.markets import STATE_EMOJI, MarketHours, MarketStatus


def _markets_from_positions(
    positions: list[Position], hours: MarketHours
) -> tuple[list[MarketStatus], dict[str, str], dict[str, MarketStatus]]:
    """Compute one MarketStatus per distinct STK exchange + flag lookup +
    by-IB-code lookup.

    CASH positions never contribute — they don't pin a venue. Unmapped
    exchange codes (status returns None) are silently dropped so an
    unknown venue doesn't crash the page.

    The third returned value maps an IB exchange code (e.g. "SEHK") to
    its MarketStatus so per-row template logic (lunch-break subtext)
    can check the state of the row's own exchange.
    """
    from app.core.symbols import flag_for_exchange

    seen: list[str] = []
    for p in positions:
        if p.asset_class != "STK":
            continue
        if p.exchange and p.exchange not in seen:
            seen.append(p.exchange)

    markets: list[MarketStatus] = []
    flag_by_display: dict[str, str] = {}
    status_by_ib_code: dict[str, MarketStatus] = {}
    for ib_exchange in seen:
        status = hours.status(ib_exchange)
        if status is None:
            continue
        markets.append(status)
        status_by_ib_code[ib_exchange] = status
        try:
            flag_by_display[status.exchange] = flag_for_exchange(ib_exchange)
        except ValueError:
            flag_by_display[status.exchange] = ""
    return markets, flag_by_display, status_by_ib_code


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _is_htmx_request(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


_STATE_STRINGS = {
    ConnectionState.CONNECTED: "connected",
    ConnectionState.RECONNECTING: "reconnecting",
    ConnectionState.DISCONNECTED: "disconnected",
}


def _state_to_string(state: ConnectionState) -> str:
    return _STATE_STRINGS[state]


def _compute_totals(positions: list[Position]) -> dict:
    """Aggregate USD market value and unrealized P&L across all positions.

    Positions with fx_unavailable=True contribute 0 — we can't honestly
    sum unknowns. The corresponding row displays — in its USD column so
    the user knows the total is incomplete.

    Returns a dict the index template can render directly. P&L sign and
    percent are pre-computed here (rather than in the template) so the
    template stays purely declarative.
    """
    total_mv_usd = sum(
        p.market_value_usd for p in positions if not p.fx_unavailable
    )
    total_pnl_usd = sum(
        p.unrealized_pnl_usd for p in positions if not p.fx_unavailable
    )
    pnl_pct = (total_pnl_usd / (total_mv_usd - total_pnl_usd) * 100.0
               if (total_mv_usd - total_pnl_usd) != 0 else 0.0)
    return {
        "mv_usd": total_mv_usd,
        "pnl_usd": total_pnl_usd,
        "pnl_pct": pnl_pct,
        "pnl_is_positive": total_pnl_usd >= 0,
    }


# Every broker the Protocol can support, in display order. Filter chips
# render this list and gray any that aren't in BROKERS_ENABLED so users
# see the dimension exists before the adapter lands.
_ALL_KNOWN_BROKERS: list[str] = ["IBKR", "Futu", "Tiger", "Longbridge"]


def _enabled_brokers() -> frozenset[str]:
    """Set of brokers that should be selectable. Driven by the
    BROKERS_ENABLED env var (comma-separated, case-insensitive). v1
    defaults to just IBKR."""
    raw = os.environ.get("BROKERS_ENABLED", "ibkr")
    by_lower = {b.lower(): b for b in _ALL_KNOWN_BROKERS}
    out: set[str] = set()
    for token in raw.split(","):
        canonical = by_lower.get(token.strip().lower())
        if canonical:
            out.add(canonical)
    return frozenset(out) or frozenset({"IBKR"})


def _apply_filters(
    positions: list[Position],
    *,
    active_account: str = "All",
    active_asset: str = "All",
    active_broker: str = "All",
) -> list[Position]:
    """Apply the three filter dimensions in turn. Returns a new list.

    Hoisted out of the route so the SSE renderer can reuse it — sharing
    one filter implementation prevents a live tick from reintroducing
    rows the user just filtered out.
    """
    out = positions
    if active_account and active_account != "All":
        out = [p for p in out if p.account_id == active_account]
    if active_asset and active_asset != "All":
        out = [p for p in out if p.asset_class == active_asset]
    if active_broker and active_broker != "All":
        out = [p for p in out if p.broker == active_broker]
    return out


def _render_rows_for_filter(
    positions: list[Position],
    active_account: str = "All",
    *,
    active_asset: str = "All",
    active_broker: str = "All",
    templates_env=None,
) -> str:
    """Render <tr> rows for the SSE payload, filtered by all dimensions.

    Live ticks must respect the same ?account= / ?asset= / ?broker=
    filters the initial page-load used — otherwise the first SSE event
    would overwrite a filtered tbody with the unfiltered set.
    `active_account` is also passed into the partial context so the
    per-row account pill is suppressed under a specific-account filter
    (the pill is redundant when the user has already drilled down).

    `templates_env` is optional; tests omit it and a fresh Jinja2
    environment is built from `app/templates/`. The production app
    reuses its own `templates.env` (registered globals included).
    """
    from jinja2 import Environment, FileSystemLoader

    from app.core.symbols import flag_for_currency, flag_for_exchange

    positions = _apply_filters(
        positions,
        active_account=active_account,
        active_asset=active_asset,
        active_broker=active_broker,
    )
    if not positions:
        return ""
    if templates_env is None:
        templates_env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True,
        )
        templates_env.globals["flag_for_exchange"] = flag_for_exchange
        templates_env.globals["flag_for_currency"] = flag_for_currency
    template = templates_env.get_template("partials/holdings_row.html")
    return "".join(
        template.render({
            "position": p,
            "flag_for_exchange": flag_for_exchange,
            "market_by_ib": {},
            "active_account": active_account or "All",
        })
        for p in positions
    )


def create_app(
    *,
    broker: Broker | None = None,
    live_positions: LivePositions | None = None,
    market_hours: MarketHours | None = None,
) -> FastAPI:
    """Build a FastAPI app with the given broker (and optional LivePositions).

    When ``broker`` is None, the app constructs an IbkrAdapter from env vars and
    connects on startup via lifespan. Tests pass a fake adapter and skip lifespan.

    ``live_positions`` is the observable in-memory snapshot the SSE handler
    streams from. If None, a fresh LivePositions is created.
    """

    store = None
    fx_service = None
    if broker is None:
        manage_lifecycle = True
    else:
        manage_lifecycle = False

    if live_positions is None:
        live_positions = LivePositions()

    if market_hours is None:
        market_hours = MarketHours()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal broker, store, fx_service
        if manage_lifecycle:
            from app.adapters.ibkr import IbkrAdapter
            from app.core.fx import FxService
            from app.db.store import Store

            data_dir = Path(os.environ.get("DATA_DIR", "./data"))
            data_dir.mkdir(parents=True, exist_ok=True)
            store = Store(data_dir / "portfolio.db")
            try:
                await store.init_schema()
            except Exception:
                pass

            # Wire the real public-API fetcher; tests pass api_fetcher=None
            # or a stub to avoid network calls.
            from app.core.fx import _default_api_fetcher
            fx_service = FxService(store=store, api_fetcher=_default_api_fetcher)
            await fx_service.start()
            app.state.fx_service = fx_service

            broker = IbkrAdapter(
                host=os.environ.get("IB_HOST", "ib-gateway"),
                port=int(os.environ.get("IB_PORT", "4003")),
                client_id=int(os.environ.get("IB_CLIENT_ID", "1")),
                store=store,
                live_positions=live_positions,
                fx_service=fx_service,
            )
            app.state.broker = broker
            # start() never raises: on initial-connect failure (e.g. the
            # gateway is still doing 2FA when the dashboard boots), it
            # transitions to RECONNECTING and keeps retrying in the background.
            await broker.start()
        yield
        if manage_lifecycle and broker is not None:
            try:
                await broker.disconnect()
            except Exception:
                pass
            if fx_service is not None:
                try:
                    await fx_service.stop()
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
    from app.core.symbols import flag_for_currency, flag_for_exchange

    templates.env.globals["flag_for_exchange"] = flag_for_exchange
    templates.env.globals["flag_for_currency"] = flag_for_currency

    def render_rows(positions: list[Position]) -> str:
        """Legacy unfiltered renderer — preserved for callers that
        pre-date the per-account filter."""
        return _render_rows_for_filter(
            positions, active_account="All", templates_env=templates.env,
        )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/healthz")
    async def healthz(request: Request):
        broker_ref = request.app.state.broker
        conn_state = await broker_ref.get_connection_state()
        state = _state_to_string(conn_state)
        if _is_htmx_request(request):
            # Only the IBKR adapter exposes current_backoff_delay() today —
            # other adapters can opt in by implementing the same shape.
            delay_getter = getattr(broker_ref, "current_backoff_delay", None)
            backoff_delay = delay_getter() if callable(delay_getter) else None
            return templates.TemplateResponse(
                request=request,
                name="partials/status_badge.html",
                context={"state": state, "backoff_delay": backoff_delay},
            )
        return JSONResponse({"ibkr": state})

    @app.post("/healthz/retry")
    async def healthz_retry(request: Request):
        """Manual retry hook for the DISCONNECTED state.

        Only acts when the adapter is genuinely DISCONNECTED — clicking on
        a CONNECTED adapter would tear down the live session, and clicking
        on RECONNECTING would spawn a parallel loop. In both cases we just
        re-render the current badge.
        """
        broker = request.app.state.broker
        conn_state = await broker.get_connection_state()
        if conn_state == ConnectionState.DISCONNECTED:
            start = getattr(broker, "start", None)
            if callable(start):
                await start()
            conn_state = await broker.get_connection_state()
        delay_getter = getattr(broker, "current_backoff_delay", None)
        backoff_delay = delay_getter() if callable(delay_getter) else None
        return templates.TemplateResponse(
            request=request,
            name="partials/status_badge.html",
            context={
                "state": _state_to_string(conn_state),
                "backoff_delay": backoff_delay,
            },
        )

    @app.get("/stream/holdings")
    async def stream_holdings(
        request: Request,
        account: str | None = None,
        asset: str | None = None,
        broker: str | None = None,
    ):
        """SSE endpoint streaming row-level deltas of the current portfolio.

        Honors the same ?account= / ?asset= / ?broker= filters the
        index route does so the first SSE event after page load can't
        overwrite a filtered tbody with unfiltered rows. Unknown /
        empty values resolve to "All".

        The HTMX SSE extension on the client connects via `sse-connect`
        and listens for named events: 'snapshot' (initial full tbody on
        connect) and 'positions' (delta — only changed rows).
        """
        live = request.app.state.live_positions
        active_account = account or "All"
        active_asset = asset or "All"
        active_broker = broker or "All"

        def filtered_render(positions: list[Position]) -> str:
            return _render_rows_for_filter(
                positions,
                active_account=active_account,
                active_asset=active_asset,
                active_broker=active_broker,
                templates_env=templates.env,
            )

        generator = stream_events(live, filtered_render)
        return EventSourceResponse(generator)

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        account: str | None = None,
        asset: str | None = None,
        broker: str | None = None,
    ):
        # Query param `broker` shadows the upstream broker variable name;
        # alias upfront so the rest of the handler reads clearly.
        broker_filter = broker
        broker = request.app.state.broker
        live = request.app.state.live_positions
        conn_state = await broker.get_connection_state()
        state = _state_to_string(conn_state)
        delay_getter = getattr(broker, "current_backoff_delay", None)
        backoff_delay = delay_getter() if callable(delay_getter) else None
        # Prefer the live, tick-updated snapshot when it has data; otherwise
        # fall back to a direct broker.get_positions() fetch (covers the slice
        # 1/2 test path where live_positions is never seeded).
        positions = live.get_all()
        if not positions and conn_state == ConnectionState.CONNECTED:
            try:
                positions = await broker.get_positions()
            except NotImplementedError:
                positions = []

        # Fetch account summaries up-front so the filter chips and any
        # per-account summary line have the data they need.
        try:
            account_summaries = await broker.get_account_summary()
        except NotImplementedError:
            account_summaries = []
        known_accounts = {s.account_id for s in account_summaries} | {
            p.account_id for p in positions
        }

        # Resolve the active account filter. None / "All" / unknown → All.
        active_account = account
        if active_account in (None, "", "All") or active_account not in known_accounts:
            active_account = "All"
            shown_positions = positions
        else:
            shown_positions = [p for p in positions if p.account_id == active_account]

        # Asset-class filter. Two real values; unknown → All. (CASH/STK
        # are the only asset_class values v1 ever produces.)
        valid_assets = {"STK", "CASH"}
        active_asset = asset
        if active_asset not in valid_assets:
            active_asset = "All"
        else:
            shown_positions = [p for p in shown_positions if p.asset_class == active_asset]

        # Broker filter. V1 only enables IBKR; the dimension is here so
        # future Futu/Tiger/Longbridge adapters slot in without UI churn.
        enabled_brokers = _enabled_brokers()
        active_broker = broker_filter
        if active_broker not in enabled_brokers:
            active_broker = "All"
        else:
            shown_positions = [p for p in shown_positions if p.broker == active_broker]

        shown_positions = sorted(
            shown_positions, key=lambda p: p.market_value_native, reverse=True,
        )
        totals = _compute_totals(shown_positions)
        # Market panel uses all visible exchanges; under a filter that's
        # the filtered set, otherwise everything.
        markets, market_flag, market_by_ib = _markets_from_positions(
            shown_positions, market_hours,
        )
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "state": state,
                "backoff_delay": backoff_delay,
                "positions": shown_positions,
                "totals": totals,
                "markets": markets,
                "market_flag": market_flag,
                "market_state_emoji": STATE_EMOJI,
                "market_by_ib": market_by_ib,
                "drawer_open": False,
                "account_summaries": account_summaries,
                "active_account": active_account,
                "active_asset": active_asset,
                "active_broker": active_broker,
                "enabled_brokers": list(enabled_brokers),
                "all_known_brokers": _ALL_KNOWN_BROKERS,
                "updated_at_iso": datetime.now(timezone.utc).isoformat(),
            },
        )

    return app


app = create_app()
