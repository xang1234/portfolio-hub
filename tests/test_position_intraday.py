"""Position.intraday_change_pct and Position.intraday_pnl_usd properties.

Surfaces today's day-of P&L on the hero and per-row % under last price.
Both properties guard against the three "no signal" cases:
  - no previous_close on file
  - no live last_price
  - last_price was filled FROM previous_close (fallback for unsubscribed
    markets — a 0% delta would be misleading noise, not a real reading)

For non-USD positions the FX rate is *backed out* of mv_usd / mv_native
so the hero "Today" total uses exactly the rate the row used — including
the FxService fallback rate. Carrying a separate fx field on Position
would be the cleaner long-term design, but the back-out keeps the change
surgical and consistent with how unrealized P&L is already presented.
"""

from app.core.broker import Position


def _pos(**overrides) -> Position:
    base = dict(
        broker="IBKR", account_id="U1", native_key="1",
        canonical_symbol="AAPL.US", native_symbol="AAPL",
        exchange="NASDAQ", currency="USD",
        name_en="Apple", asset_class="STK",
        quantity=100.0, avg_cost=150.0, last_price=210.0,
        market_value_native=21000.0, market_value_usd=21000.0,
        unrealized_pnl_native=6000.0, unrealized_pnl_usd=6000.0,
        previous_close=200.0,
    )
    base.update(overrides)
    return Position(**base)


# intraday_change_pct -----------------------------------------------------


def test_intraday_change_pct_positive_direction():
    # 210 vs 200 → +5.0 %
    p = _pos(last_price=210.0, previous_close=200.0)
    assert abs(p.intraday_change_pct - 5.0) < 1e-9


def test_intraday_change_pct_negative_direction():
    # 195 vs 200 → −2.5 %
    p = _pos(last_price=195.0, previous_close=200.0)
    assert abs(p.intraday_change_pct - (-2.5)) < 1e-9


def test_intraday_change_pct_none_when_previous_close_missing():
    p = _pos(previous_close=0.0)
    assert p.intraday_change_pct is None


def test_intraday_change_pct_none_when_last_price_missing():
    # last_price=0 is IB's "no data" — guard so we don't display a false −100 %.
    p = _pos(last_price=0.0)
    assert p.intraday_change_pct is None


def test_intraday_change_pct_none_when_last_is_fallback():
    # When last_price was filled FROM previous_close (the unsubscribed-market
    # fallback path), a 0 % delta isn't a real reading — return None so the
    # row template suppresses the chg-pct line entirely.
    p = _pos(last_price=200.0, previous_close=200.0, last_price_is_previous_close=True)
    assert p.intraday_change_pct is None


# intraday_pnl_usd --------------------------------------------------------


def test_intraday_pnl_usd_usd_position():
    # 100 * (210 - 200) * 1.0 fx = $1,000
    p = _pos(last_price=210.0, previous_close=200.0)
    assert abs(p.intraday_pnl_usd - 1000.0) < 1e-9


def test_intraday_pnl_usd_non_usd_position_backs_out_fx():
    # HKD position; mv_native = 100 * 392 = 39200 HKD; mv_usd = 5018.6 → fx ≈ 0.128
    # intraday = (392 - 380) * 100 * fx ≈ 12 * 100 * 0.128 = 153.6 USD
    p = _pos(
        currency="HKD", quantity=100.0, last_price=392.0, previous_close=380.0,
        market_value_native=39200.0, market_value_usd=5018.6,
    )
    expected = (392.0 - 380.0) * 100.0 * (5018.6 / 39200.0)
    assert abs(p.intraday_pnl_usd - expected) < 1e-6


def test_intraday_pnl_usd_zero_when_no_previous_close():
    p = _pos(previous_close=0.0)
    assert p.intraday_pnl_usd == 0.0


def test_intraday_pnl_usd_zero_when_fx_unavailable():
    # mv_usd is 0 by convention when FX is unavailable; the back-out would
    # give 0 anyway, but the explicit guard documents the intent.
    p = _pos(
        currency="JPY", quantity=100.0, last_price=2710.0, previous_close=2510.0,
        market_value_native=271000.0, market_value_usd=0.0, fx_unavailable=True,
    )
    assert p.intraday_pnl_usd == 0.0


def test_intraday_pnl_usd_zero_when_last_is_fallback():
    p = _pos(last_price=200.0, previous_close=200.0, last_price_is_previous_close=True)
    assert p.intraday_pnl_usd == 0.0


def test_intraday_pnl_usd_respects_price_magnifier():
    # Pence-quoted UK equity: prices in pence, mv/pnl in pounds → divisor 100.
    # 1000 shares * (95p - 90p) / 100 * fx(1.0 if GBP→USD synthetic equal) = £50.
    # Using USD-equivalent for simplicity: mv_native already in pounds.
    p = _pos(
        currency="GBP", quantity=1000.0, avg_cost=90.0, last_price=95.0,
        previous_close=90.0,
        market_value_native=950.0,  # 1000 * 95 / 100 pence-divisor
        market_value_usd=950.0,     # 1:1 stub fx
        price_magnifier=100,
    )
    # intraday_native = 1000 * (95 - 90) / 100 = 50 GBP → 50 USD at 1:1 fx
    assert abs(p.intraday_pnl_usd - 50.0) < 1e-9
