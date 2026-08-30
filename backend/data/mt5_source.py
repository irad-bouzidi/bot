"""MetaTrader5 read adapter. One of exactly two modules allowed to import MT5.

Runs only on the trading host. Everything downstream consumes `BarSet`/`SymbolSpec`
and never sees MT5, which is what lets the whole research stack run on a dev box
with no terminal installed.

Time handling
-------------
MT5 works in BROKER SERVER time and its Python API interprets a datetime you pass
as a wall-clock in that server timezone, while `rates['time']` comes back as
server epoch seconds. The previous code handed `datetime.fromisoformat` naive
LOCAL dates straight to `copy_rates_range`, so every requested window was
silently offset by (local - server), typically 1-3 hours.

Here the offset is measured once from a live tick, stored in the symbol sidecar,
and applied in both directions. Bars are indexed in true UTC.
"""

import time as _time
from datetime import datetime, timedelta, timezone
from typing import Optional

import MetaTrader5 as mt5  # noqa: F401  (allowed here only)
import pandas as pd

from backend.core.errors import DataUnavailable
from backend.core.types import SymbolSpec
from backend.data.market_data import BarSet, MarketData

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


def ensure_initialized():
    if not mt5.initialize():
        raise DataUnavailable(
            "MT5 initialize failed: %s\n"
            "This module only works on a host with the MetaTrader 5 terminal "
            "installed and logged in. On a dev box use FileMarketData against a "
            "cached data/ directory instead." % (mt5.last_error(),)
        )
    return True


def measure_server_utc_offset(symbol):
    # type: (str) -> int
    """Seconds to ADD to UTC to get broker server time, snapped to 15 minutes."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or not tick.time:
        return 0
    raw = tick.time - _time.time()
    return int(round(raw / 900.0) * 900)


class MT5Source(MarketData):
    def __init__(self, auto_init=True):
        self._offsets = {}
        if auto_init:
            ensure_initialized()

    # -- helpers ------------------------------------------------------------

    def _select(self, symbol):
        info = mt5.symbol_info(symbol)
        if info is None:
            raise DataUnavailable(
                "Unknown symbol %r. Check the broker's exact suffix "
                "(e.g. XAUUSDm vs XAUUSD)." % symbol
            )
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise DataUnavailable(
                "symbol_select(%r) failed: %s" % (symbol, mt5.last_error())
            )
        return mt5.symbol_info(symbol)

    def offset(self, symbol):
        # type: (str) -> int
        if symbol not in self._offsets:
            self._offsets[symbol] = measure_server_utc_offset(symbol)
        return self._offsets[symbol]

    # -- MarketData ---------------------------------------------------------

    def get_symbol_spec(self, symbol):
        info = self._select(symbol)
        return SymbolSpec(
            name=symbol,
            digits=int(info.digits),
            point=float(info.point),
            tick_size=float(info.trade_tick_size or info.point),
            tick_value=float(info.trade_tick_value),
            contract_size=float(info.trade_contract_size),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            stops_level_points=int(getattr(info, "trade_stops_level", 0) or 0),
            freeze_level_points=int(getattr(info, "trade_freeze_level", 0) or 0),
            filling_modes=int(getattr(info, "filling_mode", 0) or 0),
            currency_profit=str(info.currency_profit),
            currency_margin=str(info.currency_margin),
            typical_spread_points=float(getattr(info, "spread", 0) or 0),
            server_utc_offset_seconds=self.offset(symbol),
            captured_at=datetime.now(timezone.utc).isoformat(),
            source="mt5",
        )

    def get_bars(self, symbol, timeframe, start, end, warmup_bars=0):
        if timeframe not in TIMEFRAMES:
            raise ValueError("unsupported timeframe %r" % timeframe)
        spec = self.get_symbol_spec(symbol)
        off = timedelta(seconds=spec.server_utc_offset_seconds)

        # Widen the request so `warmup_bars` closed bars exist before `start`.
        secs = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
                "H1": 3600, "H4": 14400, "D1": 86400}[timeframe]
        pad = timedelta(seconds=int(warmup_bars * secs * 2.2))

        rates = mt5.copy_rates_range(
            symbol, TIMEFRAMES[timeframe],
            _to_server(start - pad, off), _to_server(end, off),
        )
        if rates is None or len(rates) == 0:
            raise DataUnavailable(
                "MT5 returned no %s %s bars for %s..%s (%s). The terminal may not "
                "have downloaded that history yet -- open the symbol's chart and "
                "scroll back, then retry."
                % (symbol, timeframe, start.date(), end.date(), mt5.last_error())
            )

        df = pd.DataFrame(rates)
        # rates['time'] is SERVER epoch seconds; shift it to true UTC.
        idx = pd.to_datetime(df["time"], unit="s", utc=True) - off
        df = df.drop(columns=["time"]).set_index(pd.DatetimeIndex(idx, name="time"))

        before = int((df.index < start).sum())
        warnings = []
        if warmup_bars and before < warmup_bars:
            warnings.append(
                "Requested %d warm-up bars but the broker only had %d before %s."
                % (warmup_bars, before, start)
            )
        return BarSet(
            df=df, spec=spec, symbol=symbol, timeframe=timeframe,
            warmup_count=min(before, warmup_bars), source="mt5",
            fetched_at=datetime.now(timezone.utc), warnings=warnings,
        )


def _to_server(dt_utc, off):
    # type: (datetime, timedelta) -> datetime
    """UTC -> the naive server wall-clock MT5 expects."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return (dt_utc.astimezone(timezone.utc) + off).replace(tzinfo=None)
