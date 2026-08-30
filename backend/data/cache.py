"""On-disk bar cache: monthly csv.gz shards plus a JSON spec sidecar.

csv.gz rather than parquet on purpose: pyarrow is not installed, pyarrow>=15
dropped Python 3.8, and the trading host should not need new native wheels. A
year of M5 gold is ~2 MB gzipped. The files are also human-inspectable, which
matters when reconciling against the broker.

Layout:
    data/bars/{SYMBOL}/{TIMEFRAME}/{YYYY-MM}.csv.gz
    data/specs/{SYMBOL}.json
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pandas as pd

from backend.core.errors import DataUnavailable
from backend.core.types import SymbolSpec
from backend.data.market_data import BarSet, MarketData, TIMEFRAME_SECONDS

DEFAULT_ROOT = "data"


def _next_month(d):
    # type: (datetime) -> datetime
    return datetime(d.year + (d.month == 12), (d.month % 12) + 1, 1, tzinfo=timezone.utc)


def _months(start, end):
    # type: (datetime, datetime) -> List[str]
    out = []
    cur = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    last = datetime(end.year, end.month, 1, tzinfo=timezone.utc)
    while cur <= last:
        out.append(cur.strftime("%Y-%m"))
        cur = _next_month(cur)
    return out


def _month_end(month):
    # type: (str) -> datetime
    d = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    return _next_month(d) - timedelta(seconds=1)


def shard_path(root, symbol, timeframe, month):
    # type: (str, str, str, str) -> str
    return os.path.join(root, "bars", symbol, timeframe, month + ".csv.gz")


def spec_path(root, symbol):
    # type: (str, str) -> str
    return os.path.join(root, "specs", symbol + ".json")


def write_spec(spec, root=DEFAULT_ROOT):
    # type: (SymbolSpec, str) -> str
    p = spec_path(root, spec.name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        json.dump(spec.to_dict(), fh, indent=2, sort_keys=True)
    return p


def read_spec(symbol, root=DEFAULT_ROOT):
    # type: (str, str) -> Optional[SymbolSpec]
    p = spec_path(root, symbol)
    if not os.path.exists(p):
        return None
    with open(p) as fh:
        return SymbolSpec.from_dict(json.load(fh))


def write_shard(df, root, symbol, timeframe, month):
    # type: (pd.DataFrame, str, str, str, str) -> str
    """Write one month's bars. Refuses rows that do not belong to `month`.

    MT5's copy_rates_range returns a single bar (its earliest) rather than an
    empty array when asked for a month it has no history for, which silently
    seeded bogus rows into every pre-history shard until this guard was added.
    """
    out = df.copy()
    out.index = out.index.tz_convert("UTC")
    stamps = out.index.strftime("%Y-%m")
    if len(out) and not (stamps == month).all():
        bad = sorted(set(stamps[stamps != month]))
        raise ValueError(
            "refusing to write shard %s/%s/%s: it contains rows from %s"
            % (symbol, timeframe, month, ", ".join(bad[:5]))
        )
    p = shard_path(root, symbol, timeframe, month)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    out.to_csv(p, compression="gzip", index_label="time")
    return p


def read_shard(root, symbol, timeframe, month):
    # type: (str, str, str, str) -> Optional[pd.DataFrame]
    p = shard_path(root, symbol, timeframe, month)
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p, compression="gzip", parse_dates=["time"], index_col="time")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


class FileMarketData(MarketData):
    """Cache-only source. Used by tests, CI, and the dev box (no MT5)."""

    def __init__(self, root=DEFAULT_ROOT):
        self.root = root

    def get_symbol_spec(self, symbol):
        spec = read_spec(symbol, self.root)
        if spec is None:
            raise DataUnavailable(
                "No symbol spec cached for " + symbol + ".\n"
                "Run this ON THE MT5 HOST, then copy the data/ directory back:\n"
                "    python -m backend.data.snapshot --symbol " + symbol + " --spec-only"
            )
        return spec

    def _load(self, symbol, timeframe, months):
        frames = [read_shard(self.root, symbol, timeframe, m) for m in months]
        frames = [f for f in frames if f is not None and len(f)]
        if not frames:
            return None
        df = pd.concat(frames).sort_index()
        return df[~df.index.duplicated(keep="last")]

    def _padded_months(self, timeframe, start, end, warmup_bars):
        secs = TIMEFRAME_SECONDS[timeframe]
        # Pad generously: weekends and holidays mean N bars span more than N*secs.
        pad = timedelta(seconds=int(warmup_bars * secs * 2.2) + secs)
        return _months(start - pad, end)

    def get_bars(self, symbol, timeframe, start, end, warmup_bars=0):
        months = self._padded_months(timeframe, start, end, warmup_bars)
        df = self._load(symbol, timeframe, months)
        if df is None:
            raise DataUnavailable(
                "No cached bars for {sym} {tf} in {a}..{b}.\n"
                "Run this ON THE MT5 HOST, then copy the data/ directory back:\n"
                "    python -m backend.data.snapshot --symbol {sym} "
                "--timeframe {tf} --start {a} --end {b}".format(
                    sym=symbol, tf=timeframe, a=start.date(), b=end.date()
                )
            )
        return self._assemble(df, symbol, timeframe, start, end, warmup_bars, "cache")

    def _assemble(self, df, symbol, timeframe, start, end, warmup_bars, source):
        spec = self.get_symbol_spec(symbol)
        warnings = []

        before = df[df.index < start]
        window = df[(df.index >= start) & (df.index <= end)]

        if warmup_bars > 0:
            take = before.iloc[-warmup_bars:] if len(before) else before
            if len(take) < warmup_bars:
                warnings.append(
                    "Only {got} of {want} requested warm-up bars are available "
                    "before {start}; the first indicator values in this window "
                    "will be NaN.".format(got=len(take), want=warmup_bars, start=start)
                )
        else:
            take = before.iloc[0:0]

        full = pd.concat([take, window]) if len(take) else window
        if not len(window):
            warnings.append(
                "No bars in the requested range {a}..{b}".format(a=start, b=end)
            )

        return BarSet(
            df=full, spec=spec, symbol=symbol, timeframe=timeframe,
            warmup_count=len(take), source=source,
            fetched_at=datetime.now(timezone.utc), warnings=warnings,
        )


class CachedMarketData(FileMarketData):
    """Read-through cache. Falls back to `upstream` (MT5) for missing months."""

    def __init__(self, upstream=None, root=DEFAULT_ROOT, offline=False):
        # type: (Optional[MarketData], str, bool) -> None
        FileMarketData.__init__(self, root)
        self.upstream = upstream
        self.offline = offline

    def get_symbol_spec(self, symbol):
        try:
            return FileMarketData.get_symbol_spec(self, symbol)
        except DataUnavailable:
            if self.offline or self.upstream is None:
                raise
            spec = self.upstream.get_symbol_spec(symbol)
            write_spec(spec, self.root)
            return spec

    def get_bars(self, symbol, timeframe, start, end, warmup_bars=0):
        if self.offline or self.upstream is None:
            return FileMarketData.get_bars(
                self, symbol, timeframe, start, end, warmup_bars
            )

        self.get_symbol_spec(symbol)  # populate the sidecar before shards
        months = self._padded_months(timeframe, start, end, warmup_bars)
        now_month = datetime.now(timezone.utc).strftime("%Y-%m")

        for m in months:
            have = read_shard(self.root, symbol, timeframe, m)
            # Always refetch the current month: it is still growing.
            if have is None or m == now_month:
                fetched = self.upstream.get_bars(
                    symbol, timeframe,
                    datetime.strptime(m, "%Y-%m").replace(tzinfo=timezone.utc),
                    _month_end(m), warmup_bars=0,
                )
                if len(fetched.df):
                    write_shard(fetched.df, self.root, symbol, timeframe, m)

        df = self._load(symbol, timeframe, months)
        if df is None:
            raise DataUnavailable(
                "Broker returned no {sym} {tf} bars for {a}..{b}; its history may "
                "not reach that far back.".format(
                    sym=symbol, tf=timeframe, a=start.date(), b=end.date()
                )
            )
        return self._assemble(
            df, symbol, timeframe, start, end, warmup_bars, "mt5+cache"
        )
