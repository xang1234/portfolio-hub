"""Slice 10 cycle 3: build_equity_snapshot_row pure converter.

Takes an AccountSummary + (snapshot_at, snapshot_session) and returns
the kwargs dict for Store.insert_equity_snapshot. No I/O. Lives in
app/core/equity.py so it's broker-agnostic and ready for Futu/Tiger/
Longbridge adapters to reuse.
"""

from datetime import datetime, timezone

import pytest


def _summary(**over):
    from app.core.broker import AccountSummary
    base = dict(
        broker="IBKR", account_id="U7575980", base_currency="USD",
        net_liquidation_usd=125_000.0, cash_usd=30_000.0,
        buying_power_usd=500_000.0,
        net_liquidation_native=125_000.0, gross_position_value_usd=95_000.0,
    )
    base.update(over)
    return AccountSummary(**base)


def test_builder_passes_through_all_required_fields():
    from app.core.equity import build_equity_snapshot_row

    at = datetime(2026, 5, 17, 20, 0, tzinfo=timezone.utc)
    row = build_equity_snapshot_row(
        account=_summary(), snapshot_at=at, snapshot_session="NYSE_CLOSE",
    )
    assert row["snapshot_at"] == at
    assert row["snapshot_session"] == "NYSE_CLOSE"
    assert row["broker"] == "IBKR"
    assert row["account_id"] == "U7575980"
    assert row["base_currency"] == "USD"
    assert row["net_liquidation_native"] == 125_000.0
    assert row["net_liquidation_usd"] == 125_000.0
    assert row["gross_position_value_usd"] == 95_000.0
    assert row["cash_usd"] == 30_000.0


def test_builder_uses_native_currency_for_native_value():
    """HKD account: native NLV should be the HKD figure, USD the converted one."""
    from app.core.equity import build_equity_snapshot_row

    s = _summary(
        base_currency="HKD",
        net_liquidation_native=780_000.0,
        net_liquidation_usd=100_000.0,
        gross_position_value_usd=85_000.0,
        cash_usd=15_000.0,
    )
    row = build_equity_snapshot_row(
        account=s,
        snapshot_at=datetime(2026, 5, 17, 8, 0, tzinfo=timezone.utc),
        snapshot_session="HKEX_CLOSE",
    )
    assert row["base_currency"] == "HKD"
    assert row["net_liquidation_native"] == 780_000.0
    assert row["net_liquidation_usd"] == 100_000.0


def test_builder_session_must_be_non_empty():
    """Defensive: an empty session string would let a buggy caller produce
    rows with no exchange tag, making the equity-curve UI ambiguous later."""
    from app.core.equity import build_equity_snapshot_row

    with pytest.raises(ValueError):
        build_equity_snapshot_row(
            account=_summary(),
            snapshot_at=datetime(2026, 5, 17, 20, 0, tzinfo=timezone.utc),
            snapshot_session="",
        )


def test_builder_requires_aware_snapshot_at():
    """Naive datetimes would round-trip ambiguously through SQLite — refuse."""
    from app.core.equity import build_equity_snapshot_row

    with pytest.raises(ValueError):
        build_equity_snapshot_row(
            account=_summary(),
            snapshot_at=datetime(2026, 5, 17, 20, 0),  # naive
            snapshot_session="NYSE_CLOSE",
        )
