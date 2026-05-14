"""Tests for canonical-symbol derivation and country-flag mapping.

Locked-in rules from PLAN.md:
  - Longbridge-style canonical symbols: <native>.<country_suffix>
  - HK, TW, and mainland China are STRICTLY separate (🇭🇰, 🇹🇼, 🇨🇳)
  - Unknown exchanges must raise rather than silently default to a wrong flag

The mappings live in app.core.symbols.
"""

import pytest


# canonical_symbol() -----------------------------------------------------------


def test_canonical_symbol_for_hong_kong_stock():
    from app.core.symbols import canonical_symbol

    assert canonical_symbol("700", "SEHK") == "700.HK"


def test_canonical_symbol_for_us_stock_on_nasdaq():
    from app.core.symbols import canonical_symbol

    assert canonical_symbol("AAPL", "NASDAQ") == "AAPL.US"


def test_canonical_symbol_for_us_stock_on_nyse():
    from app.core.symbols import canonical_symbol

    assert canonical_symbol("BRK B", "NYSE") == "BRK B.US"


def test_canonical_symbol_for_us_etf_on_arca():
    from app.core.symbols import canonical_symbol

    # ARCA-listed ETFs are still US instruments
    assert canonical_symbol("SPY", "ARCA") == "SPY.US"


def test_canonical_symbol_for_tokyo_stock():
    from app.core.symbols import canonical_symbol

    assert canonical_symbol("7203", "TSEJ") == "7203.JP"


def test_canonical_symbol_for_taiwan_stock_uses_tw_suffix_not_cn():
    """Taiwan must be .TW, NEVER conflated with mainland China."""
    from app.core.symbols import canonical_symbol

    assert canonical_symbol("2330", "TWSE") == "2330.TW"


def test_canonical_symbol_for_shanghai_a_share():
    from app.core.symbols import canonical_symbol

    assert canonical_symbol("600519", "SSE") == "600519.SH"


def test_canonical_symbol_for_shenzhen_a_share():
    from app.core.symbols import canonical_symbol

    assert canonical_symbol("000001", "SZSE") == "000001.SZ"


def test_canonical_symbol_for_korean_stock():
    from app.core.symbols import canonical_symbol

    assert canonical_symbol("005930", "KSE") == "005930.KR"


def test_canonical_symbol_for_australian_stock():
    from app.core.symbols import canonical_symbol

    assert canonical_symbol("BHP", "ASX") == "BHP.AU"


def test_canonical_symbol_unknown_exchange_raises():
    from app.core.symbols import canonical_symbol

    with pytest.raises(ValueError, match="unknown exchange"):
        canonical_symbol("FOO", "MARS-EXCHANGE")


# flag_for_exchange() ----------------------------------------------------------


def test_flag_for_hkex_is_hong_kong_not_china():
    """Hard requirement (saved memory): HK is never 🇨🇳."""
    from app.core.symbols import flag_for_exchange

    assert flag_for_exchange("SEHK") == "🇭🇰"


def test_flag_for_twse_is_taiwan_not_china():
    """Hard requirement (saved memory): Taiwan is never 🇨🇳."""
    from app.core.symbols import flag_for_exchange

    assert flag_for_exchange("TWSE") == "🇹🇼"


def test_flag_for_sse_is_china():
    from app.core.symbols import flag_for_exchange

    assert flag_for_exchange("SSE") == "🇨🇳"


def test_flag_for_szse_is_china():
    from app.core.symbols import flag_for_exchange

    assert flag_for_exchange("SZSE") == "🇨🇳"


def test_flag_for_us_exchanges_is_us():
    from app.core.symbols import flag_for_exchange

    for ib_exchange in ("NASDAQ", "NYSE", "ARCA", "AMEX"):
        assert flag_for_exchange(ib_exchange) == "🇺🇸", ib_exchange


def test_flag_for_unknown_exchange_raises():
    from app.core.symbols import flag_for_exchange

    with pytest.raises(ValueError, match="unknown exchange"):
        flag_for_exchange("MARS-EXCHANGE")


# Additional IB exchange codes encountered live on real accounts --------------


def test_krx_korea_exchange_maps_to_kr_suffix():
    """KRX is IB's actual code for Korea Exchange; KSE was the legacy name."""
    from app.core.symbols import canonical_symbol

    assert canonical_symbol("005930", "KRX") == "005930.KR"


def test_krx_korea_exchange_flag_is_kr():
    from app.core.symbols import flag_for_exchange

    assert flag_for_exchange("KRX") == "🇰🇷"


def test_tpex_taipei_otc_exchange_maps_to_tw_suffix_not_cn():
    """TPEX is Taiwan's OTC market, distinct from TWSE main board, but still
    a Taiwan instrument — must be 🇹🇼, NOT 🇨🇳."""
    from app.core.symbols import canonical_symbol, flag_for_exchange

    assert canonical_symbol("3105", "TPEX") == "3105.TW"
    assert flag_for_exchange("TPEX") == "🇹🇼"


def test_ibis_xetra_frankfurt_maps_to_de_suffix():
    """IBIS is IB's code for Xetra (Frankfurt)."""
    from app.core.symbols import canonical_symbol, flag_for_exchange

    assert canonical_symbol("SAP", "IBIS") == "SAP.DE"
    assert flag_for_exchange("IBIS") == "🇩🇪"


def test_sbf_euronext_paris_maps_to_fr_suffix():
    """SBF is IB's code for Euronext Paris."""
    from app.core.symbols import canonical_symbol, flag_for_exchange

    assert canonical_symbol("MC", "SBF") == "MC.FR"
    assert flag_for_exchange("SBF") == "🇫🇷"


def test_aeb_euronext_amsterdam_maps_to_nl_suffix():
    """AEB is IB's code for Euronext Amsterdam."""
    from app.core.symbols import canonical_symbol, flag_for_exchange

    assert canonical_symbol("ASML", "AEB") == "ASML.NL"
    assert flag_for_exchange("AEB") == "🇳🇱"
