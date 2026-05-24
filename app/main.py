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

import asyncio
import logging
import os
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app.core.broker import Broker, ConnectionState, Position
from app.core.equity import build_equity_snapshot_row
from app.core.live_positions import LivePositions, stream_events
from app.core.markets import (
    STATE_LABEL,
    MarketHours,
    MarketStatus,
    region_color_for_exchange,
)
from app.jobs.fills_reconcile import (
    parse_hhmm,
    reconcile_fills,
    scheduled_reconcile_loop,
)


_LOG = logging.getLogger(__name__)


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


def _log_loop_crash(name: str) -> Callable[[asyncio.Task], None]:
    """Build a done_callback that surfaces background-loop crashes on the
    app's logger. Without this, an uncaught exception in a fire-and-forget
    asyncio task lands on the default asyncio handler — logged under the
    `asyncio` logger and easy to miss when ops watch only app.*."""
    def callback(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            _LOG.error("%s loop crashed: %s", name, exc, exc_info=exc)
    return callback


def _require_admin_token(request: Request) -> None:
    """Enforce shared-secret auth on /admin/* routes when ADMIN_TOKEN is set.

    Raises HTTPException 401 on mismatch. When the env var is unset (default
    for dev / tests / Tailscale-only deployments), this is a no-op — the
    operator opts into auth by setting the variable. Read at request time
    rather than app start so the token can be rotated without restart.

    secrets.compare_digest is used instead of `==` to avoid leaking the
    token length / prefix through timing side-channels.
    """
    import secrets

    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected:
        return
    provided = request.headers.get("X-Admin-Token", "")
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")


def _compute_totals(positions: list[Position]) -> dict:
    """Aggregate USD market value and unrealized P&L across all positions.

    Positions with fx_unavailable=True contribute 0 — we can't honestly
    sum unknowns. The corresponding row displays — in its USD column so
    the user knows the total is incomplete.

    Returns a dict the index template can render directly. P&L sign and
    percent are pre-computed here (rather than in the template) so the
    template stays purely declarative.

    `has_intraday` gates the hero's "Today" row: True iff at least one
    position has both a live last_price and a populated previous_close
    (i.e. its intraday_change_pct is not None). Without that gate, a
    fresh boot with no prev-close cache would show "Today $0 (+0.0%)"
    on a hero that's actually fully populated — misleading flat reading
    that looks like real flat-day data.
    """
    total_mv_usd = sum(
        p.market_value_usd for p in positions if not p.fx_unavailable
    )
    total_pnl_usd = sum(
        p.unrealized_pnl_usd for p in positions if not p.fx_unavailable
    )
    pnl_pct = (total_pnl_usd / (total_mv_usd - total_pnl_usd) * 100.0
               if (total_mv_usd - total_pnl_usd) != 0 else 0.0)

    intraday_pnl_usd = sum(
        p.intraday_pnl_usd for p in positions if not p.fx_unavailable
    )
    # cost basis at the open ≈ today's MV − today's intraday change in USD
    intraday_basis_usd = total_mv_usd - intraday_pnl_usd
    intraday_pnl_pct = (
        intraday_pnl_usd / intraday_basis_usd * 100.0
        if intraday_basis_usd > 0 else 0.0
    )
    has_intraday = any(
        p.intraday_change_pct is not None for p in positions
    )
    return {
        "mv_usd": total_mv_usd,
        "pnl_usd": total_pnl_usd,
        "pnl_pct": pnl_pct,
        "pnl_is_positive": total_pnl_usd >= 0,
        "intraday_pnl_usd": intraday_pnl_usd,
        "intraday_pnl_pct": intraday_pnl_pct,
        "intraday_pnl_is_positive": intraday_pnl_usd >= 0,
        "has_intraday": has_intraday,
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
        templates_env.globals["region_color_for_exchange"] = region_color_for_exchange
    # region_color_for_exchange is on templates_env.globals (above), so the
    # partial can call it directly without a per-render context key.
    template = templates_env.get_template("partials/holdings_row.html")
    # Pass totals so the Weight column renders correctly on SSE-streamed
    # rows too — the partial divides position.market_value_usd by
    # totals.mv_usd. The total is computed against the filtered set so
    # weight is "share of what the user is currently looking at", not
    # share of the whole portfolio (matches the visible allocation bar).
    totals = _compute_totals(positions)
    return "".join(
        template.render({
            "position": p,
            "flag_for_exchange": flag_for_exchange,
            "market_by_ib": {},
            "active_account": active_account or "All",
            "totals": totals,
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

    reconcile_task: asyncio.Task | None = None
    snapshot_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal broker, store, fx_service, reconcile_task, snapshot_task
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
            # Expose the store so admin endpoints (fills reconcile) can find it.
            app.state.store = store

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

            # Spawn the daily fills-reconcile loop. Backstops the live
            # execDetailsEvent stream — catches anything missed during
            # disconnect windows. RECONCILE_AT_HHMM is UTC for v1 (no TZ
            # juggling); operator can shift it to off-hours for their region.
            reconcile_at = parse_hhmm(os.environ.get("RECONCILE_AT_HHMM", "23:00"))
            reconcile_task = asyncio.create_task(
                scheduled_reconcile_loop(broker, store, at=reconcile_at),
            )
            reconcile_task.add_done_callback(_log_loop_crash("fills reconcile"))

            # Spawn the equity-snapshot scheduler. Sleeps until each held
            # exchange's next regular-session close (half-day/holiday-aware
            # via exchange_calendars), captures one row per linked account
            # into equity_snapshots, then loops.
            # Reuse the create_app-level MarketHours instance so the scheduler
            # shares the (lazily-loaded) exchange_calendars cache with the
            # index route — avoids both duplicate first-call I/O and
            # divergent calendars on a hot-reload.
            from app.jobs.snapshot import scheduled_snapshot_loop
            snapshot_task = asyncio.create_task(
                scheduled_snapshot_loop(broker, store, market_hours),
            )
            snapshot_task.add_done_callback(_log_loop_crash("equity snapshot"))
        yield
        if manage_lifecycle:
            for t in (reconcile_task, snapshot_task):
                if t is not None and not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
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
    templates.env.globals["region_color_for_exchange"] = region_color_for_exchange

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

    @app.post("/admin/reconcile-fills")
    async def admin_reconcile_fills(request: Request):
        """Manual trigger for the EOD fills reconciliation.

        Useful for operators verifying the path works without waiting
        for the scheduled daily run. Returns the count of newly inserted
        rows; zero is the healthy steady-state value when the live
        execDetailsEvent stream is keeping up.

        Auth: when ADMIN_TOKEN env var is set, callers must supply the
        matching X-Admin-Token header. Unset → no auth (Tailscale is the
        network boundary in default deployments). Read at request time
        so operators can rotate the token without restarting the app.
        """
        _require_admin_token(request)
        store = getattr(request.app.state, "store", None)
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="fills store not configured on this app instance",
            )
        broker = request.app.state.broker
        inserted = await reconcile_fills(broker, store)
        return JSONResponse({"inserted": inserted})

    @app.post("/admin/snapshot")
    async def admin_snapshot(request: Request, session: str = "MANUAL"):
        """Manual trigger for an equity-snapshot capture.

        Inserts one equity_snapshots row per linked (broker, account_id),
        tagged with the supplied `?session=` (default "MANUAL"). Same
        shared-secret auth as /admin/reconcile-fills. Useful for operators
        verifying the snapshot path without waiting for an actual
        exchange close.
        """
        _require_admin_token(request)
        store = getattr(request.app.state, "store", None)
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="equity_snapshots store not configured on this app instance",
            )
        broker_ref = request.app.state.broker
        try:
            summaries = await broker_ref.get_account_summary()
        except Exception as exc:
            _LOG.warning("manual snapshot: get_account_summary failed: %s", exc)
            summaries = []
        snapshot_at = datetime.now(timezone.utc)
        inserted = 0
        for s in summaries:
            row = build_equity_snapshot_row(
                account=s, snapshot_at=snapshot_at, snapshot_session=session,
            )
            if await store.insert_equity_snapshot(**row):
                inserted += 1
        return JSONResponse({"inserted": inserted})

    @app.get("/api/equity-history")
    async def equity_history(request: Request, account: str | None = None, days: int = 60):
        """Time series of NLV (USD) for the hero sparkline.

        ?days= is clamped to [1, 400] — under one year of trading days
        is the only meaningful display window for a 220px-wide spark.
        ?account= None or "All" aggregates across every linked account;
        a specific account_id narrows to that one.

        When no Store is attached (test_create_app paths skip lifespan),
        we return [] rather than 503 so the client-side sparkline simply
        no-ops. Same for an empty history before the first snapshot fires.
        """
        days_clamped = max(1, min(400, int(days)))
        store = getattr(request.app.state, "store", None)
        if store is None:
            return JSONResponse([])
        account_filter = None if account in (None, "", "All") else account
        rows = await store.get_equity_history(
            days=days_clamped, account_id=account_filter,
        )
        return JSONResponse([
            {"t": r["snapshot_at"].isoformat(), "v": r["net_liquidation_usd"]}
            for r in rows
        ])

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
                "market_state_label": STATE_LABEL,
                "market_by_ib": market_by_ib,
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
