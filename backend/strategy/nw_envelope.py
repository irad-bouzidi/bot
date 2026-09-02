"""Nadaraya-Watson envelope mean-reversion strategy.

This is the single definition of the strategy. The live loop and the backtest
engine both drive it, which closes the defect where entry/exit rules existed
twice with different SL/TP semantics (broker-side intrabar vs close-only) and
could silently diverge.

Entry/exit modes are explicit config rather than implicit behaviour:

* `entry_mode="level"` reproduces the original `close < lower` state condition.
* `entry_mode="cross"` matches the Pine source's `ta.crossunder`/`ta.crossover`
  EVENT semantics. The two differ after a stop-out while price is still outside
  the band, where the level version immediately re-enters -- serially averaging
  into a running trend.

* `sl_mode="fixed"` is the original fixed-pip stop.
* `sl_mode="band"` sets the stop from the band half-width, which makes the
  risk/reward structural instead of an accident of the instrument's price level.
  A fixed pip stop copied across instruments is how a config ends up risking
  700 points to make 500.

* `be_trigger_mode` / `partial_fraction` add a scale-out: at a set distance in
  profit, close part of the position and pull the stop to entry. Note what this
  does and does not do -- it raises the win rate and cuts the loss column, but it
  also caps the winners it lets through, so it is a variance reduction, not free
  money. Judge it on expectancy_r and drawdown, never on win rate.
"""

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from backend.core.types import Side, Signal, SignalType
from backend.indicators.nadaraya_watson import nw_envelope, nw_warmup_bars
from backend.strategy.base import BarContext, Strategy


@dataclass(frozen=True)
class NWConfig:
    bandwidth: float = 8.0
    mult: float = 3.0
    window: int = 500
    mae_window: int = 500          # 500 = original code; 499 = Pine parity
    taps: Optional[int] = None
    atr_period: int = 14

    entry_mode: str = "level"      # "level" | "cross"
    exit_at_mean: bool = True

    sl_mode: str = "fixed"         # "fixed" | "band" | "atr"
    sl_price: float = 7.0          # fixed stop distance, PRICE units
    sl_band_k: float = 1.0         # sl = k * band half-width
    sl_atr_mult: float = 1.5

    tp_mode: str = "fixed"         # "fixed" | "r_multiple" | "none"
    tp_price: float = 10.0
    tp_r_multiple: float = 1.5     # tp = R * sl_distance -- cannot invert by accident

    # Scale-out / break-even. At `be_trigger` in profit, close `partial_fraction`
    # of the position and pull the stop to entry.
    #   "none"        disabled
    #   "tp_fraction" trigger = be_trigger_tp_fraction * tp_distance (needs a TP)
    #   "fixed"       trigger = be_trigger_price, in PRICE units
    #   "r"           trigger = be_trigger_r * sl_distance
    # tp_fraction is the default because it is the only mode that stays sane across
    # symbols and targets: 0.5 means "half way to target" whether the target is 10
    # or 50 price units, where a hardcoded 5.0 would be half the target on gold and
    # a tenth of it on a symbol quoted 10x higher.
    be_trigger_mode: str = "tp_fraction"
    be_trigger_tp_fraction: float = 0.5
    be_trigger_price: float = 5.0
    be_trigger_r: float = 0.7
    partial_fraction: float = 0.5  # 0.5 of 0.1 lots = 0.05 out, 0.05 runs to target

    min_strength: float = 0.0      # penetration depth filter, in band half-widths
    max_spread_points: Optional[float] = None


def atr(df, period=14):
    # type: (pd.DataFrame, int) -> np.ndarray
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean().values


class NWEnvelopeStrategy(Strategy):
    def __init__(self, cfg=None):
        # type: (Optional[NWConfig]) -> None
        self.cfg = cfg or NWConfig()
        if self.cfg.entry_mode not in ("level", "cross"):
            raise ValueError("entry_mode must be 'level' or 'cross'")
        if self.cfg.sl_mode not in ("fixed", "band", "atr"):
            raise ValueError("sl_mode must be 'fixed', 'band' or 'atr'")
        if self.cfg.be_trigger_mode not in ("none", "fixed", "tp_fraction", "r"):
            raise ValueError("be_trigger_mode must be 'none', 'fixed', "
                             "'tp_fraction' or 'r'")
        if not 0.0 <= self.cfg.partial_fraction < 1.0:
            raise ValueError("partial_fraction must be in [0, 1) -- 1.0 would close "
                             "the whole position and leave nothing running")

    def warmup_bars(self):
        c = self.cfg
        return max(
            nw_warmup_bars(window=c.window, mae_window=c.mae_window, taps=c.taps),
            c.atr_period,
        )

    def feature_names(self):
        return ["out", "upper", "lower", "mae", "atr", "prev_close"]

    def prepare(self, bars):
        c = self.cfg
        env = nw_envelope(
            bars["close"].values, bandwidth=c.bandwidth, mult=c.mult,
            window=c.window, mae_window=c.mae_window, taps=c.taps,
        )
        return pd.DataFrame(
            {
                "out": env.out,
                "upper": env.upper,
                "lower": env.lower,
                "mae": env.mae,
                "atr": atr(bars, c.atr_period),
                # For "cross" mode. Causal by construction: shift(1) looks BACK.
                "prev_close": bars["close"].shift(1).values,
            },
            index=bars.index,
        )

    # -- sizing of the protective levels ------------------------------------

    def _sl_distance(self, f):
        c = self.cfg
        if c.sl_mode == "fixed":
            return c.sl_price
        if c.sl_mode == "band":
            return c.sl_band_k * f["mae"]
        return c.sl_atr_mult * f["atr"]

    def _tp_distance(self, sl):
        c = self.cfg
        if c.tp_mode == "none":
            return None
        if c.tp_mode == "fixed":
            return c.tp_price
        return c.tp_r_multiple * sl

    def _be_trigger_distance(self, sl, tp):
        c = self.cfg
        if c.be_trigger_mode == "none" or c.partial_fraction <= 0:
            return None
        if c.be_trigger_mode == "fixed":
            d = c.be_trigger_price
        elif c.be_trigger_mode == "r":
            d = c.be_trigger_r * sl
        else:
            if not tp:
                return None
            d = c.be_trigger_tp_fraction * tp
        # A trigger at or past the target would never arm before the TP closed the
        # trade; the engine drops it too, but returning None keeps the ledger's
        # be_trigger_price honest about the rule that was actually in force.
        if d <= 0 or (tp and d >= tp):
            return None
        return d

    # -- the rules ----------------------------------------------------------

    def on_bar(self, ctx):
        c = self.cfg
        f = ctx.features
        close = ctx.bar.close

        if not np.isfinite(f.get("out", np.nan)) or not np.isfinite(f.get("mae", np.nan)):
            return []

        if ctx.position is not None:
            if c.exit_at_mean:
                if ctx.position.side is Side.LONG and close >= f["out"]:
                    return [Signal(SignalType.EXIT, "cross_center", close,
                                   features=dict(f))]
                if ctx.position.side is Side.SHORT and close <= f["out"]:
                    return [Signal(SignalType.EXIT, "cross_center", close,
                                   features=dict(f))]
            return []

        if (c.max_spread_points is not None
                and ctx.bar.spread_points > c.max_spread_points):
            return []

        below, above = close < f["lower"], close > f["upper"]
        if c.entry_mode == "cross":
            prev = f.get("prev_close", np.nan)
            if not np.isfinite(prev):
                return []
            # Fire only on the bar that first pierces the band.
            below = below and prev >= f["lower"]
            above = above and prev <= f["upper"]

        if not (below or above):
            return []

        band = f["mae"] if f["mae"] > 0 else np.nan
        strength = abs(close - (f["lower"] if below else f["upper"])) / band \
            if np.isfinite(band) else 0.0
        if strength < c.min_strength:
            return []

        sl = self._sl_distance(f)
        if not np.isfinite(sl) or sl <= 0:
            return []
        tp = self._tp_distance(sl)

        return [Signal(
            type=SignalType.ENTER_LONG if below else SignalType.ENTER_SHORT,
            reason="close_below_lower" if below else "close_above_upper",
            ref_price=close, sl_distance=sl, tp_distance=tp,
            strength=float(strength), features=dict(f),
            be_trigger_distance=self._be_trigger_distance(sl, tp),
            partial_fraction=c.partial_fraction,
        )]
