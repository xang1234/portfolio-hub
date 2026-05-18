"""Broker-agnostic equity-snapshot row construction.

The scheduler (app/jobs/snapshot.py) wakes at each held exchange's close,
asks the broker adapter for current AccountSummary objects, and passes
each one through this builder to get the kwargs dict that
Store.insert_equity_snapshot accepts.

Living in core/ keeps the converter independent of any specific adapter:
Futu / Tiger / Longbridge will produce AccountSummary too, and this
function works on any of them without modification.
"""

from datetime import datetime

from app.core.broker import AccountSummary


def build_equity_snapshot_row(
    *,
    account: AccountSummary,
    snapshot_at: datetime,
    snapshot_session: str,
) -> dict:
    """Project an AccountSummary into Store.insert_equity_snapshot kwargs.

    Validates two things the database can't easily catch later:
    - `snapshot_session` must be non-empty, otherwise the equity-curve
      UI can't tell rows apart by exchange.
    - `snapshot_at` must be tz-aware, otherwise SQLite ISO storage round-
      trips ambiguously across deployments in different timezones.
    """
    if not snapshot_session:
        raise ValueError("snapshot_session must be non-empty")
    if snapshot_at.tzinfo is None:
        raise ValueError("snapshot_at must be timezone-aware (UTC preferred)")
    return {
        "snapshot_at": snapshot_at,
        "snapshot_session": snapshot_session,
        "broker": account.broker,
        "account_id": account.account_id,
        "base_currency": account.base_currency,
        "net_liquidation_native": account.net_liquidation_native,
        "net_liquidation_usd": account.net_liquidation_usd,
        "gross_position_value_usd": account.gross_position_value_usd,
        "cash_usd": account.cash_usd,
    }
