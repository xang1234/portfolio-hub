"""Yahoo Finance EOD fallback for instruments where IB has no market data.

Many international exchanges (TSEJ, SBF, IBIS, SFB) are gated behind paid
IB market-data subscriptions — even historical daily data. For accounts
without those subscriptions, Yahoo's free chart API returns the previous
close at no cost.

This is an *unofficial* endpoint and could change without warning. The
fetcher is deliberately defensive: on any failure (network, parse,
missing data) it returns None and the row degrades to "—" instead of
crashing the page.

Symbol convention: Yahoo uses its own per-exchange suffix table
(`.T` for Tokyo, `.PA` for Paris Euronext, etc.). The mapping lives in
this module so it's discoverable next to the fetcher.
"""

import logging
from typing import Any


_LOG = logging.getLogger(__name__)


# IB exchange code → Yahoo suffix. Missing entries → Yahoo doesn't cover that
# venue (or we just don't know the mapping); the fallback is skipped.
#
# Source: empirical Yahoo symbol patterns. Yahoo lumps several Euronext venues
# under a single suffix (e.g., AEB/SBF/BVME all distinct on Euronext); the
# mappings here are best-effort and should be expanded as new exchanges show
# up in user portfolios.
_YAHOO_SUFFIX: dict[str, str] = {
    # Asia-Pacific
    "TSEJ": ".T",      # Tokyo
    "TSE": ".T",
    "OSE": ".T",       # Osaka (uses Tokyo Yahoo data)
    "SEHK": ".HK",     # Hong Kong
    "SEHKNTL": ".HK",  # Stock Connect Northbound (HK-routed mainland)
    "SEHKSZSE": ".HK",
    "KRX": ".KS",      # Korea KOSPI
    "KSE": ".KS",
    "KOSDAQ": ".KQ",
    "TWSE": ".TW",     # Taiwan
    "TPEX": ".TWO",    # Taipei OTC
    "SSE": ".SS",      # Shanghai
    "SZSE": ".SZ",     # Shenzhen
    "SGX": ".SI",      # Singapore
    "ASX": ".AX",      # Sydney
    # Europe
    "LSE": ".L",       # London
    "IOB": ".IL",      # International Order Book (LSE)
    "IBIS": ".DE",     # Xetra Frankfurt
    "FWB": ".F",       # Frankfurt floor
    "SBF": ".PA",      # Euronext Paris
    "AEB": ".AS",      # Euronext Amsterdam
    "BM": ".MC",       # Madrid
    "BVME": ".MI",     # Borsa Italiana Milan
    "SFB": ".ST",      # Stockholm
    "EBS": ".SW",      # Swiss
    "SIX": ".SW",
    # Americas
    "TSX": ".TO",      # Toronto
    # US has no suffix
    "NYSE": "",
    "NASDAQ": "",
    "ARCA": "",
    "AMEX": "",
    "BATS": "",
}

# Some Yahoo conventions require padding. HK tickers are 4-digit zero-padded
# on Yahoo (e.g. Tencent is "0700.HK" not "700.HK").
_PADDED_EXCHANGES: dict[str, int] = {
    "SEHK": 4,
    "SEHKNTL": 4,
    "SEHKSZSE": 4,
}


def yahoo_symbol_for(native_symbol: str, exchange: str) -> str | None:
    """Translate an IB native symbol + exchange code to a Yahoo Finance symbol.

    Returns None when we don't have a mapping for the exchange — caller
    should skip the Yahoo fallback for those rows.
    """
    suffix = _YAHOO_SUFFIX.get(exchange)
    if suffix is None:
        return None
    pad_width = _PADDED_EXCHANGES.get(exchange)
    symbol = native_symbol
    if pad_width is not None and symbol.isdigit():
        symbol = symbol.zfill(pad_width)
    return f"{symbol}{suffix}" if suffix else symbol


def extract_latest_close(payload: dict[str, Any]) -> float | None:
    """Pull the most recent non-null close from a Yahoo chart v8 response.

    Returns None for any malformed payload — the chart endpoint can return
    structurally-different errors and we want to degrade quietly in all
    cases.
    """
    try:
        result = payload["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(closes, list):
        return None
    for value in reversed(closes):
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


async def default_yahoo_fetcher(symbol: str) -> float | None:
    """Production fetcher: hits Yahoo's free chart endpoint via httpx.

    Returns the most recent daily close, or None on any failure (network
    error, parse failure, no data). The chart v8 endpoint doesn't require
    auth, but Yahoo blocks user-agents that look like bots — set a
    browser-ish UA.
    """
    import httpx

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": "5d"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return extract_latest_close(response.json())
    except Exception as exc:
        _LOG.debug("Yahoo fetch failed for %s: %s", symbol, exc)
        return None
