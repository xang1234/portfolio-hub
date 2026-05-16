"""IB returns Contract.primaryExchange codes like 'SEHK', 'TSEJ', 'IBIS'.
The exchange_calendars library expects ISO MIC codes like 'XHKG', 'XTKS',
'XETR'. mic_for_ib_exchange bridges the two.

The user's portfolio spans 15+ venues so the mapping table needs to
cover each one we've seen surface from reqContractDetails. Unknown
codes return None (the panel skips the row) rather than guessing.
"""

import pytest


# Asia-Pacific --------------------------------------------------------------


def test_sehk_maps_to_xhkg_hong_kong():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("SEHK") == "XHKG"


def test_tsej_maps_to_xtks_tokyo():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("TSEJ") == "XTKS"


def test_krx_maps_to_xkrx_korea():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("KRX") == "XKRX"


def test_kse_maps_to_xkrx_korea():
    """IB sometimes returns KSE instead of KRX for the same venue."""
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("KSE") == "XKRX"


def test_twse_maps_to_xtai_taiwan():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("TWSE") == "XTAI"


def test_tpex_taipei_otc_maps_to_xtai():
    """Taiwan OTC shares calendar with the main board."""
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("TPEX") == "XTAI"


def test_sgx_maps_to_xses_singapore():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("SGX") == "XSES"


def test_asx_maps_to_xasx_sydney():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("ASX") == "XASX"


def test_sse_maps_to_xshg_shanghai():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("SSE") == "XSHG"


# Europe --------------------------------------------------------------------


def test_lse_maps_to_xlon():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("LSE") == "XLON"


def test_ibis_xetra_maps_to_xetr():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("IBIS") == "XETR"


def test_sbf_paris_maps_to_xpar():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("SBF") == "XPAR"


def test_aeb_amsterdam_maps_to_xams():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("AEB") == "XAMS"


def test_sfb_stockholm_maps_to_xsto():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("SFB") == "XSTO"


def test_ebs_swiss_maps_to_xswx():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("EBS") == "XSWX"


# Americas ------------------------------------------------------------------


def test_nyse_maps_to_xnys():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("NYSE") == "XNYS"


def test_nasdaq_maps_to_xnys_for_calendar_purposes():
    """exchange_calendars unifies US equity calendars under XNYS."""
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("NASDAQ") == "XNYS"


def test_arca_maps_to_xnys():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("ARCA") == "XNYS"


def test_tsx_toronto_maps_to_xtse():
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("TSX") == "XTSE"


# Unknown -------------------------------------------------------------------


def test_unknown_ib_exchange_returns_none():
    """We refuse to guess — unknown codes get None so the panel skips them
    rather than misrepresenting the schedule."""
    from app.core.markets import mic_for_ib_exchange

    assert mic_for_ib_exchange("NOT_A_REAL_EXCHANGE") is None


# HK/TW/CN must be three distinct calendars --------------------------------


def test_hong_kong_taiwan_china_use_three_distinct_calendars():
    """Hard requirement: never share calendars across these three.
    They have different lunch breaks, different holidays, different
    closing times."""
    from app.core.markets import mic_for_ib_exchange

    hk = mic_for_ib_exchange("SEHK")
    tw = mic_for_ib_exchange("TWSE")
    cn = mic_for_ib_exchange("SSE")
    assert hk != tw
    assert tw != cn
    assert cn != hk
