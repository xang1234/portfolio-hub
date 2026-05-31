"""Tiger adapter configuration parsing."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class TigerConfigError(RuntimeError):
    """Tiger adapter configuration is invalid and should not be retried."""


@dataclass(frozen=True)
class TigerAdapterSettings:
    config_dir: str | None = None
    tiger_id: str | None = None
    account: str | None = None
    private_key: str | None = None
    private_key_path: str | None = None
    base_currency: str = "USD"
    markets: tuple[str, ...] = ("US", "HK", "SG", "AU", "CN")
    poll_interval_s: float = 30.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "TigerAdapterSettings":
        return cls(
            config_dir=clean_path(env_text(env, "TIGER_CONFIG_DIR")),
            tiger_id=env_text(env, "TIGER_ID"),
            account=env_text(env, "TIGER_ACCOUNT"),
            private_key=env_text(env, "TIGER_PRIVATE_KEY"),
            private_key_path=env_text(env, "TIGER_PRIVATE_KEY_PATH"),
            base_currency=(env.get("TIGER_BASE_CURRENCY", "USD") or "USD")
            .strip()
            .upper()
            or "USD",
            markets=env_list(env, "TIGER_MARKETS", "US,HK,SG,AU,CN"),
            poll_interval_s=float(env.get("TIGER_POLL_INTERVAL_S", "30") or "30"),
        )


def env_text(env: Mapping[str, str], name: str) -> str | None:
    return (env.get(name) or "").strip() or None


def env_list(env: Mapping[str, str], name: str, default: str) -> tuple[str, ...]:
    raw = env.get(name, default) or default
    return tuple(part.strip().upper() for part in raw.split(",") if part.strip())


def clean_path(value: str | None) -> str | None:
    text_value = (value or "").strip()
    return str(Path(text_value).expanduser()) if text_value else None
