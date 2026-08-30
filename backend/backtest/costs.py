"""Trading costs. The previous backtest modelled none of this.

Spread convention
-----------------
MT5 OHLC bars are BID prices. A long enters at ask (bid + spread) and exits at
bid; a short enters at bid and exits at ask. Either way a round trip pays exactly
ONE spread, not two. Getting this wrong by a factor of two is enough to kill an
otherwise viable configuration, so `test_costs.py` pins it.

`spread_source="bar"` uses the broker's real per-bar `spread` column, which MT5
returns for free alongside OHLC -- including the session-open and news blowouts
that matter most to a band-fading strategy. That is strictly better than assuming
a constant.

Slippage is deliberately asymmetric. A stop is hit precisely during a fast
adverse move, so SL exits fill worse than average. Modelling entry and exit
slippage with the same distribution is the classic way a stop-based backtest
flatters itself.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from backend.core.types import Side, SymbolSpec


@dataclass(frozen=True)
class CostConfig:
    spread_source: str = "bar"                 # "bar" | "fixed" | "none"
    fixed_spread_points: float = 0.0
    spread_multiplier: float = 1.0             # scenario knob: P25 / P75 / P95
    commission_per_lot_round_turn: float = 0.0
    slippage_points_entry: float = 0.0
    slippage_points_exit: float = 0.0
    slippage_points_stop: float = 0.0          # extra, applied to SL exits only
    swap_long_points_per_day: float = 0.0
    swap_short_points_per_day: float = 0.0
    triple_swap_weekday: int = 2               # Wednesday


class CostModel:
    def __init__(self, cfg=None, spec=None):
        # type: (Optional[CostConfig], Optional[SymbolSpec]) -> None
        self.cfg = cfg or CostConfig()
        self.spec = spec

    def spread_price(self, spec, bar_spread_points):
        # type: (SymbolSpec, float) -> float
        c = self.cfg
        if c.spread_source == "none":
            return 0.0
        pts = c.fixed_spread_points if c.spread_source == "fixed" else bar_spread_points
        return max(0.0, float(pts)) * c.spread_multiplier * spec.point

    def entry_fill(self, side, base_price, spec, bar_spread_points):
        # type: (Side, float, SymbolSpec, float) -> float
        """Bars are bid. A long pays the spread on entry; a short does not."""
        s = self.spread_price(spec, bar_spread_points)
        slip = self.cfg.slippage_points_entry * spec.point
        return base_price + s + slip if side is Side.LONG else base_price - slip

    def exit_fill(self, side, base_price, spec, bar_spread_points, is_stop=False):
        # type: (Side, float, SymbolSpec, float, bool) -> float
        """A short pays the spread on exit; a long does not."""
        s = self.spread_price(spec, bar_spread_points)
        slip = self.cfg.slippage_points_exit
        if is_stop:
            slip += self.cfg.slippage_points_stop
        slip *= spec.point
        return base_price - slip if side is Side.LONG else base_price + s + slip

    def commission(self, volume):
        # type: (float) -> float
        return self.cfg.commission_per_lot_round_turn * volume

    def swap(self, side, volume, open_time, close_time, spec):
        # type: (Side, float, datetime, datetime, SymbolSpec) -> float
        pts = (self.cfg.swap_long_points_per_day if side is Side.LONG
               else self.cfg.swap_short_points_per_day)
        if not pts:
            return 0.0
        nights = 0
        cur = open_time
        while cur.date() < close_time.date():
            cur = cur.replace(hour=0, minute=0, second=0, microsecond=0)
            cur += _ONE_DAY
            if cur > close_time:
                break
            nights += 3 if cur.weekday() == self.cfg.triple_swap_weekday else 1
        if not nights:
            return 0.0
        per_night = pts * spec.point / spec.tick_size * spec.tick_value * volume
        return per_night * nights


from datetime import timedelta as _td  # noqa: E402

_ONE_DAY = _td(days=1)
