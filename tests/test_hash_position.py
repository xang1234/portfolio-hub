"""Tests for hash_position — the change-detection key for SSE deltas.

The hash function compresses a Position's *display-relevant* fields into a
short string. The SSE handler tracks last-seen hashes per row per client; only
rows whose hash differs from the last push get included in the next delta.

Fields that affect display (and thus must be in the hash):
  - quantity, last_price, market_value_native, market_value_usd
  - unrealized_pnl_native, unrealized_pnl_usd
  - name_en, currency

Fields that DON'T affect display (and should NOT change the hash):
  - native_key (stable IB conId)
  - broker, account_id, canonical_symbol (used as the row key, not value)
  - avg_cost (not currently displayed — slice 8 expands the detail card)
"""


def _new_position(**overrides):
    from app.core.broker import Position

    base = dict(
        broker="IBKR",
        account_id="U7575980",
        native_key="76792991",
        canonical_symbol="700.HK",
        native_symbol="700",
        exchange="SEHK",
        currency="HKD",
        name_en="TENCENT HOLDINGS LTD",
        asset_class="STK",
        quantity=100.0,
        avg_cost=400.0,
        last_price=420.0,
        market_value_native=42000.0,
        market_value_usd=0.0,
        unrealized_pnl_native=2000.0,
        unrealized_pnl_usd=0.0,
    )
    base.update(overrides)
    return Position(**base)


def test_same_position_yields_same_hash():
    from app.core.live_positions import hash_position

    a = _new_position()
    b = _new_position()
    assert hash_position(a) == hash_position(b)


def test_different_last_price_yields_different_hash():
    from app.core.live_positions import hash_position

    assert hash_position(_new_position(last_price=420.0)) != hash_position(_new_position(last_price=421.0))


def test_different_quantity_yields_different_hash():
    from app.core.live_positions import hash_position

    assert hash_position(_new_position(quantity=100.0)) != hash_position(_new_position(quantity=200.0))


def test_different_market_value_native_yields_different_hash():
    from app.core.live_positions import hash_position

    assert hash_position(_new_position(market_value_native=1.0)) != hash_position(_new_position(market_value_native=2.0))


def test_different_market_value_usd_yields_different_hash():
    """Slice 3 will populate this; the hash must already include it so
    FX-driven recomputes fan out to all rows in that currency."""
    from app.core.live_positions import hash_position

    assert hash_position(_new_position(market_value_usd=1.0)) != hash_position(_new_position(market_value_usd=2.0))


def test_different_name_en_yields_different_hash():
    """Name resolution might complete asynchronously after first render,
    triggering a re-push of that row."""
    from app.core.live_positions import hash_position

    assert hash_position(_new_position(name_en="A")) != hash_position(_new_position(name_en="B"))


def test_different_unrealized_pnl_native_yields_different_hash():
    from app.core.live_positions import hash_position

    assert hash_position(_new_position(unrealized_pnl_native=100.0)) != hash_position(_new_position(unrealized_pnl_native=200.0))


def test_different_avg_cost_alone_does_not_change_hash():
    """avg_cost is not in the v1 display; changes to it (rare — usually only
    after a buy/sell trade) shouldn't force a re-push."""
    from app.core.live_positions import hash_position

    assert hash_position(_new_position(avg_cost=400.0)) == hash_position(_new_position(avg_cost=405.0))


def test_different_native_key_does_not_change_hash():
    """native_key is internal plumbing, not displayed."""
    from app.core.live_positions import hash_position

    assert hash_position(_new_position(native_key="1")) == hash_position(_new_position(native_key="2"))


def test_last_price_is_stale_flag_changes_hash():
    """Slice 9: stale flag must be in the hash so the SSE delta layer emits
    when a row goes stale (during the reconnect window) and again when a
    real tick clears the flag. Without this, the ⚠️ badge would only show
    on the next price tick by coincidence, defeating the visual."""
    from app.core.live_positions import hash_position

    assert hash_position(_new_position(last_price_is_stale=False)) != hash_position(
        _new_position(last_price_is_stale=True)
    )


def test_hash_is_short_string():
    """Used in per-client memory dicts; should be a small, stable string."""
    from app.core.live_positions import hash_position

    h = hash_position(_new_position())
    assert isinstance(h, str)
    assert len(h) <= 32  # short SHA prefix is sufficient
