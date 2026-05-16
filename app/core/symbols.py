"""Canonical symbol + country-flag mappings.

Two locked-in rules from PLAN.md:
  - Longbridge-style suffixes: <native>.<country_suffix>
  - HK (🇭🇰), Taiwan (🇹🇼), and mainland China (🇨🇳) are ALWAYS rendered as
    three separate entities. Never silently fall back to a default flag for
    an unknown exchange — raise instead.
"""

# IB primaryExchange code → country/region suffix used in canonical_symbol.
IB_EXCHANGE_TO_SUFFIX: dict[str, str] = {
    # United States
    "NASDAQ": "US",
    "NYSE": "US",
    "ARCA": "US",
    "AMEX": "US",
    "BATS": "US",
    "IEX": "US",
    "PINK": "US",
    "ISLAND": "US",
    # Hong Kong (NEVER grouped with mainland China). Stock Connect
    # northbound listings (SEHKNTL, SEHKSZSE) trade on HKEX with HK
    # session hours; still flagged 🇭🇰 even though the underlying
    # is a mainland A-share.
    "SEHK": "HK",
    "HKEX": "HK",
    "SEHKNTL": "HK",
    "SEHKSZSE": "HK",
    # Taiwan (NEVER grouped with mainland China)
    "TWSE": "TW",
    "TPEX": "TW",      # Taiwan OTC market (distinct from TWSE main board)
    # Mainland China
    "SSE": "SH",
    "SZSE": "SZ",
    # Japan. IB uses both "TSEJ" and bare "TSE" for the Tokyo
    # Stock Exchange depending on the contract.
    "TSEJ": "JP",
    "TSE": "JP",
    "OSE": "JP",
    # South Korea
    "KSE": "KR",       # legacy name
    "KRX": "KR",       # current IB code for Korea Exchange
    "KOSDAQ": "KR",
    # Australia
    "ASX": "AU",
    # United Kingdom
    "LSE": "UK",
    "IOB": "UK",
    "LSEETF": "UK",
    # Singapore
    "SGX": "SG",
    # Switzerland
    "EBS": "CH",
    "SIX": "CH",
    # Canada
    "TSX": "CA",
    "TSXV": "CA",
    # Germany
    "IBIS": "DE",      # Xetra / Frankfurt
    "FWB": "DE",
    # France
    "SBF": "FR",       # Euronext Paris
    # Netherlands
    "AEB": "NL",       # Euronext Amsterdam
    # Spain
    "BM": "ES",        # Bolsa de Madrid
    # Italy
    "BVME": "IT",      # Borsa Italiana
    # Sweden
    "SFB": "SE",       # Stockholmsbörsen (Nasdaq Stockholm)
}

# Country-suffix → flag emoji.
_SUFFIX_TO_FLAG: dict[str, str] = {
    "US": "🇺🇸",
    "HK": "🇭🇰",
    "TW": "🇹🇼",
    "SH": "🇨🇳",
    "SZ": "🇨🇳",
    "JP": "🇯🇵",
    "KR": "🇰🇷",
    "AU": "🇦🇺",
    "UK": "🇬🇧",
    "SG": "🇸🇬",
    "CH": "🇨🇭",
    "CA": "🇨🇦",
    "DE": "🇩🇪",
    "FR": "🇫🇷",
    "NL": "🇳🇱",
    "ES": "🇪🇸",
    "IT": "🇮🇹",
    "SE": "🇸🇪",
}


def canonical_symbol(native_symbol: str, primary_exchange: str) -> str:
    """Return a Longbridge-style canonical symbol like "700.HK" or "AAPL.US".

    Raises ValueError if the primary_exchange is not recognized — silently
    defaulting would risk mis-flagging Hong Kong / Taiwan instruments as
    mainland Chinese.
    """
    try:
        suffix = IB_EXCHANGE_TO_SUFFIX[primary_exchange]
    except KeyError:
        raise ValueError(f"unknown exchange: {primary_exchange!r}") from None
    return f"{native_symbol}.{suffix}"


def flag_for_exchange(primary_exchange: str) -> str:
    """Return the country/region flag emoji for an IB primary exchange code.

    Raises ValueError for unknown exchanges (see module docstring).
    """
    try:
        suffix = IB_EXCHANGE_TO_SUFFIX[primary_exchange]
    except KeyError:
        raise ValueError(f"unknown exchange: {primary_exchange!r}") from None
    return _SUFFIX_TO_FLAG[suffix]
