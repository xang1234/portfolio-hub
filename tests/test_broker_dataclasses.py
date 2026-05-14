"""Tests for the Position and AccountSummary dataclasses + extended Broker Protocol.

Slice 2 adds:
  - Position dataclass with all the fields PLAN.md specifies
  - AccountSummary dataclass
  - Broker.get_positions() to the Protocol
  - Broker.get_account_summary() to the Protocol (returns list)

USD-related fields exist but can default to 0.0 (filled in slice 3).
"""

import inspect


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


# Position ---------------------------------------------------------------------


def test_position_has_all_required_fields():
    p = _new_position()

    assert p.broker == "IBKR"
    assert p.account_id == "U7575980"
    assert p.native_key == "76792991"
    assert p.canonical_symbol == "700.HK"
    assert p.native_symbol == "700"
    assert p.exchange == "SEHK"
    assert p.currency == "HKD"
    assert p.name_en == "TENCENT HOLDINGS LTD"
    assert p.asset_class == "STK"
    assert p.quantity == 100.0
    assert p.avg_cost == 400.0
    assert p.last_price == 420.0
    assert p.market_value_native == 42000.0
    assert p.market_value_usd == 0.0
    assert p.unrealized_pnl_native == 2000.0
    assert p.unrealized_pnl_usd == 0.0


def test_position_supports_stk_asset_class():
    p = _new_position(asset_class="STK")
    assert p.asset_class == "STK"


def test_position_supports_cash_asset_class():
    p = _new_position(asset_class="CASH", native_symbol="HKD", currency="HKD")
    assert p.asset_class == "CASH"


# AccountSummary ---------------------------------------------------------------


def test_account_summary_dataclass_has_required_fields():
    from app.core.broker import AccountSummary

    s = AccountSummary(
        broker="IBKR",
        account_id="U7575980",
        base_currency="USD",
        net_liquidation_usd=123456.78,
        cash_usd=10000.0,
        buying_power_usd=246913.56,
    )
    assert s.broker == "IBKR"
    assert s.account_id == "U7575980"
    assert s.base_currency == "USD"
    assert s.net_liquidation_usd == 123456.78
    assert s.cash_usd == 10000.0
    assert s.buying_power_usd == 246913.56


# Broker Protocol extensions ---------------------------------------------------


def test_broker_protocol_declares_get_positions():
    from app.core.broker import Broker

    assert hasattr(Broker, "get_positions")
    assert inspect.iscoroutinefunction(Broker.get_positions)


def test_broker_protocol_declares_get_account_summary():
    from app.core.broker import Broker

    assert hasattr(Broker, "get_account_summary")
    assert inspect.iscoroutinefunction(Broker.get_account_summary)
