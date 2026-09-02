"""
SpeedTrader AI — Configuration Layer
Spec refs: §115 Configuration Layer, §116 Configuration Isolation, §117 Change Control

Configuration is loaded once, frozen, and never written to at runtime.
§116: an LLM cannot silently modify risk limits, the kill switch, portfolio limits,
strategy parameters or execution restrictions. The mechanism is not a policy note —
it is that these objects are immutable and nothing in the agent path holds a setter.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

CONFIG_DIR = Path(os.environ.get("SPEEDTRADER_CONFIG_DIR", "configs"))

_REQUIRED = (
    "risk_config",
    "strategy_config",
    "portfolio_config",
    "agent_config",
    "model_config",
    "execution_config",
)

# Keys that must exist and be sane, or the system refuses to start (§111 fail closed).
_RISK_INVARIANTS = (
    "risk_per_trade_pct",
    "daily_loss_limit_pct",
    "portfolio_heat_max_pct",
    "min_score",
    "max_open_positions",
    "fail_closed",
)


def _freeze(obj: Any) -> Any:
    """Deep freeze. Dicts become read-only mappings, lists become tuples."""
    if isinstance(obj, dict):
        return MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return tuple(_freeze(v) for v in obj)
    return obj


def _load_one(name: str) -> Mapping[str, Any]:
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Missing required config '{path}'. Fail closed: refusing to start with defaults. "
            f"Set SPEEDTRADER_CONFIG_DIR if your configs live elsewhere."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping.")
    return _freeze(data)


def _validate_risk(risk: Mapping[str, Any]) -> None:
    missing = [k for k in _RISK_INVARIANTS if k not in risk]
    if missing:
        raise ValueError(f"risk_config is missing required keys: {missing}")

    if risk["fail_closed"] is not True:
        raise ValueError(
            "risk_config.fail_closed must be true. §113: the system must never "
            "interpret missing safety information as approval."
        )
    if not 0 < risk["risk_per_trade_pct"] <= 5:
        raise ValueError("risk_per_trade_pct outside sane bounds (0, 5].")
    if risk["portfolio_heat_max_pct"] <= risk.get("heat_reduce_level_pct", 0):
        raise ValueError("portfolio_heat_max_pct must exceed heat_reduce_level_pct.")
    if risk["daily_loss_limit_pct"] >= risk.get("weekly_loss_limit_pct", 1e9):
        raise ValueError("daily_loss_limit_pct must be below weekly_loss_limit_pct.")


class Config:
    """Frozen container for all six config domains (§115)."""

    __slots__ = ("risk", "strategy", "portfolio", "agent", "model", "execution")

    def __init__(self) -> None:
        object.__setattr__(self, "risk", _load_one("risk_config"))
        object.__setattr__(self, "strategy", _load_one("strategy_config"))
        object.__setattr__(self, "portfolio", _load_one("portfolio_config"))
        object.__setattr__(self, "agent", _load_one("agent_config"))
        object.__setattr__(self, "model", _load_one("model_config"))
        object.__setattr__(self, "execution", _load_one("execution_config"))
        _validate_risk(self.risk)

    def __setattr__(self, *_: Any) -> None:
        raise AttributeError(
            "Configuration is immutable at runtime (§116). "
            "Edit the YAML and restart — changes are an engineering action (§117)."
        )

    def enabled_strategies(self) -> tuple[str, ...]:
        return tuple(
            sid for sid, s in self.strategy["strategies"].items() if s.get("enabled")
        )

    def model_for(self, agent_id: str) -> str:
        tier = self.model["routing"].get(agent_id, "fast")
        return self.model["tiers"][tier]


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Single process-wide instance. Loaded once, never reloaded."""
    return Config()
