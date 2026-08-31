"""Shared value types. Deliberately free of any MetaTrader5 import.

Python 3.8: use typing.Optional/List/Dict, never `X | Y`.
"""

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


class Side(enum.Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self):
        # type: () -> int
        return 1 if self is Side.LONG else -1


class SignalType(enum.Enum):
    NONE = "none"
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"


@dataclass(frozen=True)
class SymbolSpec:
    """Contract specification, captured from MT5 `symbol_info`.

    This replaces the hand-rolled `profit_mult` constant (100 for XAU, 1 for BTC),
    which was a hardcoded copy of `trade_contract_size` that silently breaks on a
    broker or symbol change and cannot express a non-USD quote currency.
    """

    name: str
    digits: int = 2
    point: float = 0.01
    tick_size: float = 0.01
    tick_value: float = 1.0          # account currency per tick, per 1.0 lot
    contract_size: float = 100.0
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    stops_level_points: int = 0
    freeze_level_points: int = 0
    filling_modes: int = 0
    currency_profit: str = "USD"
    currency_margin: str = "USD"
    typical_spread_points: float = 0.0
    server_utc_offset_seconds: int = 0
    captured_at: str = ""
    source: str = "sidecar"

    def pl(self, entry, exit_price, side, volume):
        # type: (float, float, Side, float) -> float
        """Profit in account currency. The single P&L implementation.

        Uses MT5's own tick form rather than a fudge factor, so it stays correct
        when tick_value != contract_size * tick_size (cross-currency symbols).
        """
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive for %s" % self.name)
        ticks = (exit_price - entry) * side.sign / self.tick_size
        return ticks * self.tick_value * volume

    def round_price(self, price):
        # type: (float) -> float
        step = self.tick_size or self.point
        if step and step > 0:
            price = round(price / step) * step
        return round(price, self.digits)

    def round_volume(self, volume):
        # type: (float) -> float
        step = self.volume_step or 0.01
        vol = round(volume / step) * step
        vol = max(self.volume_min, min(self.volume_max, vol))
        # step is often 0.01; guard against binary float dust like 0.060000000000000005
        return round(vol, 8)

    def min_stop_distance(self):
        # type: () -> float
        return (self.stops_level_points or 0) * self.point

    def to_dict(self):
        # type: () -> Dict[str, Any]
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        # type: (Dict[str, Any]) -> "SymbolSpec"
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass(frozen=True)
class Signal:
    """Strategy intent. Carries DISTANCES, never absolute prices.

    Keeping prices out of the strategy is what lets the same signal drive both the
    simulated and the live broker: each fills at its own price and owns its own
    rounding and stops-level clamping.

    `be_trigger_distance` and `partial_fraction` describe the SCALE-OUT rule the
    same way: a favourable distance from entry, and a proportion of the position,
    never a lot count. The live broker turns the fraction into lots against the
    symbol's volume_step; the engine turns it into lots against BacktestConfig.
    Expressing it as lots here would fuse strategy with execution and would break
    the moment the position size changed.
    """

    type: SignalType
    reason: str
    ref_price: float
    sl_distance: Optional[float] = None
    tp_distance: Optional[float] = None
    strength: float = 0.0
    features: Dict[str, float] = field(default_factory=dict)
    be_trigger_distance: Optional[float] = None
    partial_fraction: float = 0.0


@dataclass(frozen=True)
class PositionView:
    ticket: int
    symbol: str
    side: Side
    volume: float
    entry_price: float
    entry_time: datetime
    sl: float = 0.0
    tp: float = 0.0
