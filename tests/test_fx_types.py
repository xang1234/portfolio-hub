"""Slice 3 cycle 1: pure types and constants for the FX subsystem.

FxRate carries everything a row needs to render USD columns honestly:
the rate itself, when it was quoted, whether the staleness window has
elapsed, and which source produced it (so the 📡 fallback badge can
appear on rows backed by the public API instead of IB).

The CNY/CNH distinction is a hard requirement: IB returns CNH for the
offshore renminbi used in HK-routed Stock Connect A-shares. CNY is the
onshore mainland rate and is NOT a valid substitute — silent
substitution would corrupt USD valuations. We fail loudly at the
boundary instead.
"""

from datetime import datetime, timezone

import pytest


# FxRate dataclass shape ------------------------------------------------------


def test_fx_rate_can_be_instantiated_with_required_fields():
    from app.core.fx import FxRate

    rate = FxRate(
        pair="HKDUSD",
        rate=0.1283,
        quoted_at=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        is_stale=False,
        source="IB",
    )

    assert rate.pair == "HKDUSD"
    assert rate.rate == 0.1283
    assert rate.is_stale is False
    assert rate.source == "IB"


def test_fx_rate_source_can_be_api_fallback():
    from app.core.fx import FxRate

    rate = FxRate(
        pair="JPYUSD",
        rate=0.0064,
        quoted_at=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        is_stale=False,
        source="API_FALLBACK",
    )

    assert rate.source == "API_FALLBACK"


# SUPPORTED_FX --------------------------------------------------------------


def test_supported_fx_includes_all_eleven_currencies_from_plan():
    from app.core.fx import SUPPORTED_FX

    expected = {"HKD", "JPY", "KRW", "TWD", "CNH", "AUD", "GBP", "EUR", "SGD", "CHF", "CAD"}
    assert SUPPORTED_FX == expected


def test_supported_fx_includes_cnh_not_cny():
    """Hard requirement: CNH (offshore RMB, what IB returns for Stock Connect
    A-shares) is supported. CNY (onshore mainland RMB) is NOT — silent
    substitution would mis-value HKEX-routed A-share positions."""
    from app.core.fx import SUPPORTED_FX

    assert "CNH" in SUPPORTED_FX
    assert "CNY" not in SUPPORTED_FX


# validate_currency -----------------------------------------------------------


def test_validate_currency_accepts_usd():
    """USD is the base currency — convert(amount, 'USD') just returns amount."""
    from app.core.fx import validate_currency

    validate_currency("USD")  # no raise


def test_validate_currency_accepts_each_supported_code():
    from app.core.fx import SUPPORTED_FX, validate_currency

    for code in SUPPORTED_FX:
        validate_currency(code)  # no raise


def test_validate_currency_rejects_cny_with_clear_message():
    """The error message must mention both CNY (the bad input) and CNH (the
    expected alternative) so the operator can diagnose at a glance."""
    from app.core.fx import validate_currency

    with pytest.raises(ValueError) as excinfo:
        validate_currency("CNY")

    msg = str(excinfo.value)
    assert "CNY" in msg
    assert "CNH" in msg


def test_validate_currency_rejects_unsupported_code():
    """A typo'd / unsupported currency should fail loudly rather than be
    silently dropped from FX coverage."""
    from app.core.fx import validate_currency

    with pytest.raises(ValueError):
        validate_currency("XYZ")
