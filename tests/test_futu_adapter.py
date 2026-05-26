import pytest
import threading

from app.core.broker import ConnectionState
from app.core.fx import FxRate


class _Frame:
    def __init__(self, records):
        self._records = list(records)

    def to_dict(self, orient):
        assert orient == "records"
        return list(self._records)


class _Sdk:
    RET_OK = 0

    class TrdEnv:
        REAL = "REAL"
        SIMULATE = "SIMULATE"

    class TrdMarket:
        HK = "HK"
        US = "US"
        SG = "SG"

    class SecurityFirm:
        FUTUSG = "FUTUSG"

    class Currency:
        USD = "USD"


class _Context:
    def __init__(self, *, accounts=None, positions=None, funds=None, fail=False, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self._accounts = accounts or []
        self._positions = positions or {}
        self._funds = funds or {}
        self._fail = fail

    def get_acc_list(self):
        if self._fail:
            return 1, "OpenD unavailable"
        return _Sdk.RET_OK, _Frame(self._accounts)

    def position_list_query(self, *, acc_id=0, **kwargs):
        if self._fail:
            return 1, "positions unavailable"
        return _Sdk.RET_OK, _Frame(self._positions.get(str(acc_id), []))

    def accinfo_query(self, *, acc_id=0, **kwargs):
        if self._fail:
            return 1, "funds unavailable"
        return _Sdk.RET_OK, _Frame([self._funds.get(str(acc_id), {})])

    def close(self):
        self.closed = True


class _Fx:
    def get_rate_sync(self, currency):
        if currency == "HKD":
            return FxRate(
                pair="HKDUSD",
                rate=0.128,
                quoted_at=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ),
                is_stale=False,
                source="IB",
            )
        return None


@pytest.mark.asyncio
async def test_futu_adapter_maps_moomoo_position_rows_to_positions():
    from app.adapters.futu import FutuAdapter

    ctx = _Context(
        accounts=[
            {
                "acc_id": 281756479345015383,
                "trd_env": "REAL",
                "acc_status": "ACTIVE",
                "trdmarket_auth": ["HK", "US"],
            }
        ],
        positions={
            "281756479345015383": [
                {
                    "code": "HK.01810",
                    "stock_name": "XIAOMI-W",
                    "position_market": "HK",
                    "qty": 400.0,
                    "currency": "HKD",
                    "nominal_price": 49.4,
                    "average_cost": 53.975,
                    "cost_price": 53.975,
                    "market_val": 19760.0,
                    "pl_val": -1830.0,
                    "pl_val_valid": True,
                    "position_id": 6596101776329286054,
                }
            ]
        },
    )

    adapter = FutuAdapter(
        host="opend",
        port=11111,
        markets=("HK",),
        security_firm="FUTUSG",
        sdk=_Sdk,
        context_factory=lambda **kwargs: ctx,
        fx_service=_Fx(),
    )

    await adapter.connect()
    positions = await adapter.get_positions()

    assert len(positions) == 1
    p = positions[0]
    assert p.broker == "Futu"
    assert p.account_id == "281756479345015383"
    assert p.native_key == "HK.01810"
    assert p.canonical_symbol == "1810.HK"
    assert p.native_symbol == "1810"
    assert p.exchange == "SEHK"
    assert p.currency == "HKD"
    assert p.name_en == "XIAOMI-W"
    assert p.quantity == pytest.approx(400.0)
    assert p.avg_cost == pytest.approx(53.975)
    assert p.last_price == pytest.approx(49.4)
    assert p.market_value_native == pytest.approx(19760.0)
    assert p.market_value_usd == pytest.approx(2529.28)
    assert p.unrealized_pnl_native == pytest.approx(-1830.0)
    assert p.unrealized_pnl_usd == pytest.approx(-234.24)


@pytest.mark.asyncio
async def test_futu_adapter_returns_account_summary_from_accinfo_query():
    from app.adapters.futu import FutuAdapter

    ctx = _Context(
        accounts=[
            {
                "acc_id": 281756479345015383,
                "trd_env": "REAL",
                "acc_status": "ACTIVE",
            }
        ],
        funds={
            "281756479345015383": {
                "total_assets": 125000.0,
                "cash": 15000.0,
                "power": 30000.0,
                "market_val": 110000.0,
                "currency": "USD",
            }
        },
    )

    adapter = FutuAdapter(
        host="opend",
        port=11111,
        markets=("HK",),
        security_firm="FUTUSG",
        sdk=_Sdk,
        context_factory=lambda **kwargs: ctx,
    )

    await adapter.connect()
    summaries = await adapter.get_account_summary()

    assert len(summaries) == 1
    s = summaries[0]
    assert s.broker == "Futu"
    assert s.account_id == "281756479345015383"
    assert s.base_currency == "USD"
    assert s.net_liquidation_usd == pytest.approx(125000.0)
    assert s.cash_usd == pytest.approx(15000.0)
    assert s.buying_power_usd == pytest.approx(30000.0)
    assert s.net_liquidation_native == pytest.approx(125000.0)
    assert s.gross_position_value_usd == pytest.approx(110000.0)


@pytest.mark.asyncio
async def test_futu_start_degrades_without_crashing_when_opend_unavailable():
    from app.adapters.futu import FutuAdapter

    adapter = FutuAdapter(
        host="opend",
        port=11111,
        markets=("HK",),
        security_firm="FUTUSG",
        sdk=_Sdk,
        context_factory=lambda **kwargs: _Context(fail=True, **kwargs),
    )

    await adapter.start()

    assert await adapter.get_connection_state() is ConnectionState.DISCONNECTED
    assert await adapter.get_positions() == []


@pytest.mark.asyncio
async def test_futu_disconnect_closes_all_contexts():
    from app.adapters.futu import FutuAdapter

    contexts = []

    def factory(**kwargs):
        ctx = _Context(accounts=[], **kwargs)
        contexts.append(ctx)
        return ctx

    adapter = FutuAdapter(
        host="opend",
        port=11111,
        markets=("HK", "US"),
        security_firm="FUTUSG",
        sdk=_Sdk,
        context_factory=factory,
    )

    await adapter.connect()
    await adapter.disconnect()

    assert contexts
    assert all(ctx.closed for ctx in contexts)


@pytest.mark.asyncio
async def test_futu_connect_seeds_live_positions_snapshot():
    from app.adapters.futu import FutuAdapter
    from app.core.live_positions import LivePositions

    live = LivePositions()
    ctx = _Context(
        accounts=[{"acc_id": 281756479345015383, "trd_env": "REAL", "acc_status": "ACTIVE"}],
        positions={
            "281756479345015383": [
                {
                    "code": "US.AAPL",
                    "stock_name": "APPLE",
                    "position_market": "US",
                    "qty": 3.0,
                    "currency": "USD",
                    "nominal_price": 200.0,
                    "average_cost": 150.0,
                    "market_val": 600.0,
                    "pl_val": 150.0,
                }
            ]
        },
    )
    adapter = FutuAdapter(
        host="opend",
        port=11111,
        markets=("US",),
        security_firm="FUTUSG",
        sdk=_Sdk,
        context_factory=lambda **kwargs: ctx,
        live_positions=live,
        poll_interval_s=0,
    )

    await adapter.connect()

    rows = live.get_all()
    assert len(rows) == 1
    assert rows[0].broker == "Futu"
    assert rows[0].canonical_symbol == "AAPL.US"


@pytest.mark.asyncio
async def test_futu_queries_all_market_contexts_for_same_account():
    from app.adapters.futu import FutuAdapter

    account_id = "281756479345015383"
    contexts = {
        "HK": _Context(
            accounts=[{"acc_id": account_id, "trd_env": "REAL", "acc_status": "ACTIVE"}],
            positions={
                account_id: [
                    {
                        "code": "HK.01810",
                        "stock_name": "XIAOMI-W",
                        "position_market": "HK",
                        "qty": 400.0,
                        "currency": "HKD",
                        "nominal_price": 49.4,
                        "average_cost": 53.975,
                        "market_val": 19760.0,
                        "pl_val": -1830.0,
                    }
                ]
            },
        ),
        "US": _Context(
            accounts=[{"acc_id": account_id, "trd_env": "REAL", "acc_status": "ACTIVE"}],
            positions={
                account_id: [
                    {
                        "code": "US.AAPL",
                        "stock_name": "APPLE",
                        "position_market": "US",
                        "qty": 3.0,
                        "currency": "USD",
                        "nominal_price": 200.0,
                        "average_cost": 150.0,
                        "market_val": 600.0,
                        "pl_val": 150.0,
                    }
                ]
            },
        ),
    }

    adapter = FutuAdapter(
        host="opend",
        port=11111,
        markets=("HK", "US"),
        security_firm="FUTUSG",
        sdk=_Sdk,
        context_factory=lambda **kwargs: contexts[kwargs["filter_trdmarket"]],
        fx_service=_Fx(),
    )

    await adapter.connect()
    positions = await adapter.get_positions()

    assert {p.canonical_symbol for p in positions} == {"1810.HK", "AAPL.US"}


@pytest.mark.asyncio
async def test_futu_refresh_failure_preserves_last_known_live_rows():
    from app.adapters.futu import FutuAdapter
    from app.core.live_positions import LivePositions

    live = LivePositions()
    ctx = _Context(
        accounts=[{"acc_id": 281756479345015383, "trd_env": "REAL", "acc_status": "ACTIVE"}],
        positions={
            "281756479345015383": [
                {
                    "code": "US.AAPL",
                    "stock_name": "APPLE",
                    "position_market": "US",
                    "qty": 3.0,
                    "currency": "USD",
                    "nominal_price": 200.0,
                    "average_cost": 150.0,
                    "market_val": 600.0,
                    "pl_val": 150.0,
                }
            ]
        },
    )
    adapter = FutuAdapter(
        host="opend",
        port=11111,
        markets=("US",),
        security_firm="FUTUSG",
        sdk=_Sdk,
        context_factory=lambda **kwargs: ctx,
        live_positions=live,
        poll_interval_s=0,
    )
    await adapter.connect()
    assert [p.canonical_symbol for p in live.get_all()] == ["AAPL.US"]

    ctx._fail = True

    with pytest.raises(RuntimeError):
        await adapter._refresh_live_positions()

    assert [p.canonical_symbol for p in live.get_all()] == ["AAPL.US"]
    assert await adapter.get_connection_state() is ConnectionState.DISCONNECTED


@pytest.mark.asyncio
async def test_futu_poll_loop_does_not_clear_rows_after_refresh_failure():
    from app.adapters.futu import FutuAdapter
    from app.core.live_positions import LivePositions

    live = LivePositions()
    ctx = _Context(
        accounts=[{"acc_id": 281756479345015383, "trd_env": "REAL", "acc_status": "ACTIVE"}],
        positions={
            "281756479345015383": [
                {
                    "code": "US.AAPL",
                    "stock_name": "APPLE",
                    "position_market": "US",
                    "qty": 3.0,
                    "currency": "USD",
                    "nominal_price": 200.0,
                    "average_cost": 150.0,
                    "market_val": 600.0,
                    "pl_val": 150.0,
                }
            ]
        },
    )
    adapter = FutuAdapter(
        host="opend",
        port=11111,
        markets=("US",),
        security_firm="FUTUSG",
        sdk=_Sdk,
        context_factory=lambda **kwargs: ctx,
        live_positions=live,
        poll_interval_s=0.01,
    )
    await adapter.connect()

    ctx._fail = True
    await __import__("asyncio").sleep(0.035)

    assert [p.canonical_symbol for p in live.get_all()] == ["AAPL.US"]
    assert await adapter.get_connection_state() is ConnectionState.DISCONNECTED

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_futu_context_construction_runs_off_event_loop_thread():
    from app.adapters.futu import FutuAdapter

    loop_thread_id = threading.get_ident()
    factory_thread_ids: list[int] = []

    def factory(**kwargs):
        factory_thread_ids.append(threading.get_ident())
        return _Context(accounts=[], **kwargs)

    adapter = FutuAdapter(
        host="opend",
        port=11111,
        markets=("HK", "US"),
        security_firm="FUTUSG",
        sdk=_Sdk,
        context_factory=factory,
    )

    await adapter.connect()

    assert factory_thread_ids
    assert all(thread_id != loop_thread_id for thread_id in factory_thread_ids)
