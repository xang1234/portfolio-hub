"""CASH positions render their currency name as the row "name" — so we
need a stable English label for every currency we support. The currency
also needs a country/region flag that's distinct from any exchange flag
mapping (HKD → 🇭🇰, JPY → 🇯🇵, CNH → 🇨🇳, etc.).

CNH vs CNY: IB reports A-share Stock Connect cash balances as CNH
(offshore renminbi). The two are distinct currencies but share the same
🇨🇳 flag. CNY is *intentionally not* in CURRENCY_NAMES — it should
raise so we don't silently substitute CNH (the FX validation already
enforces this at the adapter boundary).
"""

import pytest

from app.core.symbols import CURRENCY_NAMES, flag_for_currency


# Currency-name lookup ----------------------------------------------------


def test_currency_names_covers_all_supported_currencies():
    """SUPPORTED_FX from slice 3 plus USD itself, so every cash balance
    the FX service can convert has a display name."""
    expected = {"HKD", "JPY", "KRW", "TWD", "CNH", "AUD", "GBP", "EUR",
                "SGD", "CHF", "CAD", "SEK", "USD"}

    assert expected.issubset(CURRENCY_NAMES.keys())


@pytest.mark.parametrize("code,expected", [
    ("HKD", "Hong Kong Dollar"),
    ("JPY", "Japanese Yen"),
    ("USD", "US Dollar"),
    ("EUR", "Euro"),
    ("GBP", "British Pound"),
    ("SGD", "Singapore Dollar"),
    ("AUD", "Australian Dollar"),
    ("CHF", "Swiss Franc"),
    ("CAD", "Canadian Dollar"),
    ("SEK", "Swedish Krona"),
    ("TWD", "Taiwan Dollar"),
    ("KRW", "South Korean Won"),
])
def test_currency_name_spelling(code, expected):
    assert CURRENCY_NAMES[code] == expected


def test_cnh_is_offshore_renminbi_not_just_yuan():
    """Match IB's terminology — CNH is the offshore (Hong-Kong-deliverable)
    renminbi used for Stock Connect, distinct from CNY."""
    name = CURRENCY_NAMES["CNH"]

    assert "CNH" in name or "Offshore" in name or "Renminbi" in name


def test_cny_is_not_in_currency_names():
    """CNY is intentionally unsupported — see slice 3's validate_currency.
    Silently mapping it to CNH would risk misrepresenting onshore vs
    offshore A-share holdings."""
    assert "CNY" not in CURRENCY_NAMES


# Currency-flag lookup ----------------------------------------------------


@pytest.mark.parametrize("code,flag", [
    ("HKD", "🇭🇰"),
    ("JPY", "🇯🇵"),
    ("USD", "🇺🇸"),
    ("EUR", "🇪🇺"),
    ("GBP", "🇬🇧"),
    ("SGD", "🇸🇬"),
    ("AUD", "🇦🇺"),
    ("CHF", "🇨🇭"),
    ("CAD", "🇨🇦"),
    ("SEK", "🇸🇪"),
    ("TWD", "🇹🇼"),  # Taiwan dollar — never grouped with mainland China
    ("KRW", "🇰🇷"),
    ("CNH", "🇨🇳"),
])
def test_flag_for_currency_returns_expected_emoji(code, flag):
    assert flag_for_currency(code) == flag


def test_hkd_twd_cnh_use_three_distinct_flags():
    """Hard plan rule: Hong Kong, Taiwan, and mainland China are always
    rendered as three separate entities. The currency flags must respect
    that just as the exchange flags do."""
    hkd = flag_for_currency("HKD")
    twd = flag_for_currency("TWD")
    cnh = flag_for_currency("CNH")

    assert hkd != twd
    assert twd != cnh
    assert cnh != hkd


def test_flag_for_unknown_currency_raises():
    """We refuse to invent a flag for an unknown currency — that would
    risk mis-flagging a holding."""
    with pytest.raises(ValueError):
        flag_for_currency("XYZ")
