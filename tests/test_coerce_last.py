"""Tests for the _coerce_last helper that pulls a usable last-price out of an
ib_async Ticker.

When markets are closed (or the account has no live data subscription),
Ticker.last is often None. The display still needs a meaningful number, so
we fall back through: last -> close -> marketPrice() -> 0.0.

This ensures Slice 2's holdings table shows the previous close when the
market is shut, rather than rendering everything as 0.00.
"""

import math


class FakeTicker:
    def __init__(self, *, last=None, close=None, market_price_value=None):
        self.last = last
        self.close = close
        self._mp = market_price_value

    def marketPrice(self):  # noqa: N802 — matches ib_async surface
        return self._mp


def test_returns_last_when_present():
    from app.adapters.ibkr import _coerce_last

    assert _coerce_last(FakeTicker(last=420.5)) == 420.5


def test_falls_back_to_close_when_last_is_none():
    from app.adapters.ibkr import _coerce_last

    assert _coerce_last(FakeTicker(last=None, close=418.0)) == 418.0


def test_falls_back_to_market_price_when_last_and_close_are_none():
    from app.adapters.ibkr import _coerce_last

    assert _coerce_last(FakeTicker(last=None, close=None, market_price_value=415.0)) == 415.0


def test_returns_zero_when_all_fields_unavailable():
    from app.adapters.ibkr import _coerce_last

    assert _coerce_last(FakeTicker(last=None, close=None, market_price_value=None)) == 0.0


def test_treats_nan_last_as_missing():
    from app.adapters.ibkr import _coerce_last

    # ib_async represents "no data" as NaN on the last field
    assert _coerce_last(FakeTicker(last=math.nan, close=418.0)) == 418.0


def test_treats_negative_one_as_missing_for_close():
    """IB sometimes returns -1.0 to indicate "no quote available"."""
    from app.adapters.ibkr import _coerce_last

    assert _coerce_last(FakeTicker(last=None, close=-1.0, market_price_value=415.0)) == 415.0
