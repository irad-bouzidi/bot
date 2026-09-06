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
and applied in both directions. Bars are indexed in true UTC. A tick is only
current while its market is OPEN, so `measure_server_utc_offset` refuses an
implausible reading rather than trusting a Friday tick on a Sunday -- see its
docstring for the corruption that taught us that.
"""

import time as _time
from datetime import datetime, timedelta, timezone
from typing import Optional

import MetaTrader5 as mt5  # noqa: F401  (allowed here only)
import pandas as pd

from backend.core.errors import DataUnavailable
from backend.core.symbols import SUPPORTED_SYMBOLS
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


# Real broker clocks live between UTC-12 and UTC+14, so a measurement outside
# that range is not a distant server -- it is a STALE TICK, i.e. a closed market.
_MAX_PLAUSIBLE_OFFSET = 14 * 3600


def _tick_offset(symbol):
    # type: (str) -> Optional[int]
    """Offset measured from `symbol`'s last tick, or None if it is not current."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or not tick.time:
        return None
    raw = tick.time - _time.time()
    if abs(raw) > _MAX_PLAUSIBLE_OFFSET:
        return None
    return int(round(raw / 900.0) * 900)


def measure_server_utc_offset(symbol, fallback=None):
    # type: (str, Optional[int]) -> int
    """Seconds to ADD to UTC to get broker server time, snapped to 15 minutes.

    The measurement comes from the symbol's last tick, which is only current
    while that symbol's market is OPEN. Ask gold on a Sunday and the newest tick
    is Friday's close, so a bare `tick.time - now()` reads as an offset of about
    -41 hours -- and every bar written against it is stamped 41 hours out.

    That is not hypothetical. It is how `data/specs/XAUUSDm.json` came to be
    marked "mt5+offset-repaired" by hand, and the repair did not survive: the
    next weekend snapshot silently wrote -148500 back into the sidecar and
    stamped a fresh H1 capture at :15 past every hour.

    So an implausible reading is treated as "could not measure" rather than as a
    number. We then try the other configured symbols -- a 24/7 instrument reads
    the SAME server clock and is still ticking on a Sunday -- and only then fall
    back to the offset already stored for this symbol. Never to a guess.
    """
    off = _tick_offset(symbol)
    if off is not None:
        return off
    for other in SUPPORTED_SYMBOLS:
        if other != symbol:
            off = _tick_offset(other)
            if off is not None:
                return off
    return 0 if fallback is None else int(fallback)


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

    def offset(self, symbol, fallback=None):
        # type: (str, Optional[int]) -> int
        """Cached per source. `fallback` is the sidecar's stored offset, used only
        when no configured symbol has a current tick -- i.e. everything is shut."""
        if symbol not in self._offsets:
            self._offsets[symbol] = measure_server_utc_offset(symbol, fallback)
        return self._offsets[symbol]

    # -- MarketData ---------------------------------------------------------

    def get_symbol_spec(self, symbol, offset_fallback=None):
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
            swap_mode=int(getattr(info, "swap_mode", 0) or 0),
            swap_long=float(getattr(info, "swap_long", 0.0) or 0.0),
            swap_short=float(getattr(info, "swap_short", 0.0) or 0.0),
            swap_rollover_3days=int(getattr(info, "swap_rollover3days", 3) or 3),
            server_utc_offset_seconds=self.offset(symbol, offset_fallback),
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
