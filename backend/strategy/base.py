"""Strategy interface shared by the backtest engine and the live loop.

The seam that matters: `on_bar` receives `features` as a Mapping of SCALARS at a
single bar index. It is structurally impossible to read index i+1, so look-ahead
cannot be written by accident -- and `test_no_lookahead` verifies the property
rather than trusting the reviewer.

The other rule: signals carry DISTANCES, never absolute prices. The simulated and
live brokers fill at different prices and must each do their own rounding and
stops-level clamping, so a strategy that computed `sl = price - 70*pip` would
already have fused strategy with execution.
"""

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import List, Mapping, Optional

import pandas as pd

from backend.core.types import PositionView, Signal, SymbolSpec


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    spread_points: float = 0.0


@dataclass(frozen=True)
class BarContext:
    index: int
    time: datetime
    bar: Bar
    features: Mapping[str, float]      # scalars at `index` only
    position: Optional[PositionView]
    spec: SymbolSpec


class Strategy(abc.ABC):
    @abc.abstractmethod
    def warmup_bars(self):
        # type: () -> int
        """Bars of history needed before the first valid signal."""

    @abc.abstractmethod
    def feature_names(self):
        # type: () -> List[str]
        ...

    @abc.abstractmethod
    def prepare(self, bars):
        # type: (pd.DataFrame) -> pd.DataFrame
        """Vectorised, CAUSAL precompute. Returns features aligned to bars.index."""

    @abc.abstractmethod
    def on_bar(self, ctx):
        # type: (BarContext) -> List[Signal]
        ...

    def reset(self):
        # type: () -> None
        pass
