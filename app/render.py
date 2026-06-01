"""Presentation layer: turn Position snapshots into the HTML fragments the
web routes and the SSE stream serve.

Extracted from main.py so the aggregation + row/hero rendering live in one
cohesive place (and main.py stays under its size budget). These are pure
functions of (positions, filters) → str; the FastAPI app re-exports the ones
tests reach for.
"""

from pathlib import Path

from app.core.broker import Position
from app.core.markets import region_color_for_exchange
from app.core.symbols import flag_for_exchange

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _fallback_env():
    """A standalone Jinja env with the globals the partials call.

    Used when no app-managed env is supplied (tests and direct callers). The
    production app passes its own `templates.env`, which already has these
    globals registered.
    """
    from jinja2 import Environment, FileSystemLoader

    from app.core.symbols import flag_for_currency

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True,
    )
    env.globals["flag_for_exchange"] = flag_for_exchange
    env.globals["flag_for_currency"] = flag_for_currency
    env.globals["region_color_for_exchange"] = region_color_for_exchange
    return env


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


def render_stream_payload(
    positions: list[Position],
    active_account: str = "All",
    *,
    active_asset: str = "All",
    active_broker: str = "All",
    templates_env=None,
) -> str:
    """Render one SSE push: holdings rows followed by the hero OOB fragment.

    Filters and aggregates the snapshot once, then renders a single template
    that emits the rows and the out-of-band hero totals from that shared
    state. The hero ("Total exposure" / "Today") lives outside the
    #positions-tbody swap target, so the OOB fragment is what keeps it live;
    rendering both from one filtered+aggregated pass keeps them consistent.
    """
    positions = _apply_filters(
        positions,
        active_account=active_account,
        active_asset=active_asset,
        active_broker=active_broker,
    )
    env = templates_env or _fallback_env()
    totals = _compute_totals(positions)
    template = env.get_template("partials/stream_payload.html")
    return template.render({
        "positions": positions,
        "totals": totals,
        "active_account": active_account or "All",
        "market_by_ib": {},
    })
