"""Market data abstraction. No MetaTrader5 import here -- see mt5_source.py."""

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from backend.core.types import SymbolSpec

BAR_COLUMNS = ["open", "high", "low", "close", "tick_volume", "spread", "real_volume"]

TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400,
}


@dataclass
class BarSet:
    """Bars plus everything needed to interpret them.

    `warmup_count` is the number of leading rows that exist ONLY to prime
    indicators. The engine evaluates `eval_slice()`, never `df`. This is the fix
    for the silent warm-up loss: previously the backtest fetched exactly the
    requested range, so the first ~998 bars of every window produced NaN bands and
    were skipped without any warning -- a "Last Week" M5 run silently became a
    three-day run.
    """

    df: pd.DataFrame
    spec: SymbolSpec
    symbol: str
    timeframe: str
    warmup_count: int = 0
    source: str = "cache"
    fetched_at: Optional[datetime] = None
    warnings: List[str] = field(default_factory=list)

    def eval_slice(self):
        # type: () -> pd.DataFrame
        return self.df.iloc[self.warmup_count:]

    def __len__(self):
        return len(self.df)

    def meta(self):
        # type: () -> Dict[str, Any]
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars_total": len(self.df),
            "bars_evaluated": len(self.df) - self.warmup_count,
            "warmup_count": self.warmup_count,
            "source": self.source,
            "start": str(self.df.index[0]) if len(self.df) else None,
            "end": str(self.df.index[-1]) if len(self.df) else None,
            "warnings": list(self.warnings),
        }


class MarketData(abc.ABC):
    @abc.abstractmethod
    def get_bars(self, symbol, timeframe, start, end, warmup_bars=0):
        # type: (str, str, datetime, datetime, int) -> BarSet
        """Bars in [start, end], preceded by `warmup_bars` rows before `start`."""

    @abc.abstractmethod
    def get_symbol_spec(self, symbol):
        # type: (str) -> SymbolSpec
        ...
