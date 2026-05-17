"""Slice 9: FX subscriptions must be rebuilt after reconnect.

The issue calls out "re-subscribe ALL active reqMktData lines (positions + FX
pairs)". Position re-subscribe is covered in test_reconnect_resubscribe.py;
this file covers the FX half.

FxService keeps its own dict of subscribed tickers (`_tickers`). On reconnect
the IB instance is replaced, so the old ticker handles point at a dead session
— if we kept them in the dict, `ensure_subscribed()` would treat them as
already-live and never re-call reqMktData on the fresh IB session, and FX
rates would silently stop updating after the daily restart.
"""

import asyncio
from dataclasses import dataclass

import pytest

from app.core.live_positions import LivePositions


class _Event:
    def __init__(self):
        self._callbacks = []
    def __iadd__(self, cb):
        self._callbacks.append(cb); return self
    def __isub__(self, cb):
        if cb in self._callbacks: self._callbacks.remove(cb)
        return self
    def emit(self, *args, **kwargs):
        for cb in list(self._callbacks): cb(*args, **kwargs)


@dataclass
class FakeContract:
    conId: int
    symbol: str
    secType: str
    currency: str
    exchange: str = "SMART"
    primaryExchange: str = ""


@dataclass
class FakeContractDetails:
    contract: FakeContract
    longName: str


@dataclass
class FakeIBPosition:
    account: str
    contract: FakeContract
    position: float
    avgCost: float


class FakeTicker:
    def __init__(self, contract, last=None):
        self.contract = contract
        self.last = last
        self.close = None
        self.updateEvent = _Event()

    def marketPrice(self):
        return self.last


class FakeIB:
    def __init__(self, positions, contract_details, last_prices=None):
        self._positions = positions
        self._contract_details = contract_details
        self._last_prices = last_prices or {}
        self._connected = False
        self.disconnectedEvent = _Event()
        # Track per-symbol so the test can assert FX pairs separately from
        # equity contracts.
        self.req_mkt_data_symbols: list[str] = []
        self.connect_attempts = 0

    async def connectAsync(self, host, port, clientId):
        self.connect_attempts += 1
        self._connected = True

    def disconnect(self): self._connected = False
    def isConnected(self): return self._connected
    def reqMarketDataType(self, mdt): pass

    async def reqPositionsAsync(self): return self._positions

    async def reqContractDetailsAsync(self, contract):
        d = self._contract_details.get(contract.conId)
        return [d] if d is not None else []

    async def reqTickersAsync(self, *contracts):
        return [FakeTicker(c, last=self._last_prices.get(c.conId)) for c in contracts]

    def reqMktData(self, contract, generic="", snapshot=False, regulatory_snapshot=False):
        sym = getattr(contract, "symbol", "?")
        self.req_mkt_data_symbols.append(sym)
        return FakeTicker(contract, last=self._last_prices.get(getattr(contract, "conId", -1)))

    def cancelMktData(self, contract): pass

    def simulate_disconnect(self):
        self._connected = False
        self.disconnectedEvent.emit()


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store
    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def _tencent():
    contract = FakeContract(conId=76792991, symbol="700", secType="STK", currency="HKD")
    details = FakeContractDetails(
        contract=FakeContract(
            conId=76792991, symbol="700", secType="STK", currency="HKD",
            primaryExchange="SEHK",
        ),
        longName="TENCENT HOLDINGS LTD",
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=100.0, avgCost=400.0)
    return pos, details


async def test_fx_pairs_resubscribed_after_reconnect(store):
    """After a disconnect → reconnect cycle, the FX HKDUSD subscription must
    be re-created against the fresh IB session. We assert by counting how
    many times reqMktData was called with the HKD forex symbol."""
    from app.adapters.ibkr import IbkrAdapter
    from app.core.fx import FxService

    pos, details = _tencent()
    fake_ib = FakeIB(
        positions=[pos],
        contract_details={76792991: details},
        last_prices={76792991: 420.0},
    )

    def forex_factory(currency):
        # Mirror real ib_async Forex contract shape closely enough for the FxService.
        return FakeContract(
            conId=-1, symbol=currency, secType="CASH", currency="USD",
            exchange="IDEALPRO",
        )

    fx = FxService(store=store, forex_factory=forex_factory)
    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: fake_ib,
        store=store,
        live_positions=live,
        fx_service=fx,
        reconnect_delays=[0.01, 0.01, 0.01],
    )

    await adapter.connect()
    # After initial connect, ensure_subscribed should have been called for HKD.
    initial_hkd_subs = fake_ib.req_mkt_data_symbols.count("HKD")
    assert initial_hkd_subs >= 1, (
        f"expected ≥1 HKDUSD subscription after initial connect, "
        f"got reqMktData calls: {fake_ib.req_mkt_data_symbols}"
    )

    fake_ib.simulate_disconnect()
    await asyncio.sleep(0.1)

    # After reconnect, the HKD pair must have been resubscribed on the fresh
    # IB instance — total HKD reqMktData calls strictly greater than before.
    assert fake_ib.req_mkt_data_symbols.count("HKD") > initial_hkd_subs, (
        f"FX pair HKDUSD was not re-subscribed after reconnect. "
        f"reqMktData calls: {fake_ib.req_mkt_data_symbols}"
    )
