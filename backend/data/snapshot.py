"""Snapshot broker history to a portable on-disk cache.

RUN THIS ON THE MT5 HOST, then copy the resulting `data/` directory to wherever
you do research. Everything downstream (backtests, baseline, sweeps,
walk-forward) then runs offline and reproducibly, with no terminal.

    python -m backend.data.snapshot --symbol XAUUSDm --start 2023-01-01
    python -m backend.data.snapshot --symbol BTCUSDm --start 2023-01-01
    python -m backend.data.snapshot --list
    python -m backend.data.snapshot --symbol XAUUSDm --verify 2026-07

The same pass also captures the contract spec, the measured server<->UTC offset,
and the broker's real per-bar spread history -- which is what the cost model
needs, and is far better than assuming a constant spread.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from backend.data.cache import (
    DEFAULT_ROOT, _months, _month_end, read_shard, shard_path, write_shard,
    write_spec,
)
from backend.data.market_data import TIMEFRAME_SECONDS


def _utc(s):
    # type: (str) -> datetime
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def cmd_snapshot(args):
    from backend.data.mt5_source import MT5Source  # imported late: needs a terminal

    src = MT5Source()
    spec = src.get_symbol_spec(args.symbol)
    path = write_spec(spec, args.root)
    print("spec  -> %s" % path)
    print("        contract_size=%s tick_size=%s tick_value=%s digits=%s"
          % (spec.contract_size, spec.tick_size, spec.tick_value, spec.digits))
    print("        stops_level=%spt filling_modes=%s spread=%spt"
          % (spec.stops_level_points, spec.filling_modes, spec.typical_spread_points))
    print("        server-UTC offset = %+d h"
          % (spec.server_utc_offset_seconds / 3600.0))
    if args.spec_only:
        return 0

    start, end = _utc(args.start), _utc(args.end)
    total = 0
    now_month = datetime.now(timezone.utc).strftime("%Y-%m")

    for m in _months(start, end):
        existing = read_shard(args.root, args.symbol, args.timeframe, m)
        if existing is not None and m != now_month and not args.force:
            print("skip  %s (%d bars cached)" % (m, len(existing)))
            total += len(existing)
            continue
        m_start = datetime.strptime(m, "%Y-%m").replace(tzinfo=timezone.utc)
        m_end = _month_end(m)
        try:
            bs = src.get_bars(args.symbol, args.timeframe, m_start, m_end)
        except Exception as exc:
            print("MISS  %s: %s" % (m, str(exc).splitlines()[0]))
            continue

        # For a month the broker has no history for, copy_rates_range does NOT
        # return an empty array -- it returns a single bar, the earliest one it
        # holds. Writing that unfiltered put one bogus row into every pre-history
        # shard. Keep only rows that genuinely belong to this month.
        part = bs.df[(bs.df.index >= m_start) & (bs.df.index <= m_end)]
        if not len(part):
            print("MISS  %s: no history" % m)
            continue

        p = write_shard(part, args.root, args.symbol, args.timeframe, m)
        total += len(part)
        print("write %s  %6d bars  -> %s" % (m, len(part), p))

    print("\ntotal %d bars cached for %s %s" % (total, args.symbol, args.timeframe))
    return 0


def cmd_list(args):
    root = os.path.join(args.root, "bars")
    if not os.path.isdir(root):
        print("no cache at %s" % root)
        return 1
    for symbol in sorted(os.listdir(root)):
        for tf in sorted(os.listdir(os.path.join(root, symbol))):
            d = os.path.join(root, symbol, tf)
            months = sorted(f[:-7] for f in os.listdir(d) if f.endswith(".csv.gz"))
            if not months:
                continue
            counts, first, last = [], None, None
            for m in months:
                df = read_shard(args.root, symbol, tf, m)
                counts.append(len(df))
                if first is None:
                    first = df.index[0]
                last = df.index[-1]
            secs = TIMEFRAME_SECONDS.get(tf, 300)
            span = (last - first).total_seconds()
            expected = int(span / secs) + 1
            got = sum(counts)
            print("%s %s: %d bars, %s .. %s (%d months)"
                  % (symbol, tf, got, first.date(), last.date(), len(months)))
            print("    coverage %.1f%% of a continuous 24/7 grid "
                  "(gaps are normal for weekends/holidays)"
                  % (100.0 * got / max(expected, 1)))
    return 0


def cmd_verify(args):
    """Re-fetch one month and diff it against the cache.

    Brokers do revise history. Without this, a "reproducible" result can quietly
    stop reproducing.
    """
    from backend.data.mt5_source import MT5Source

    m = args.verify
    cached = read_shard(args.root, args.symbol, args.timeframe, m)
    if cached is None:
        print("no cached shard for %s %s %s" % (args.symbol, args.timeframe, m))
        return 1
    src = MT5Source()
    m_start = datetime.strptime(m, "%Y-%m").replace(tzinfo=timezone.utc)
    fresh = src.get_bars(args.symbol, args.timeframe, m_start, _month_end(m)).df

    joined = cached.join(fresh, how="outer", lsuffix="_c", rsuffix="_f")
    missing = int(joined["close_c"].isna().sum() + joined["close_f"].isna().sum())
    both = joined.dropna(subset=["close_c", "close_f"])
    diff = (both["close_c"] - both["close_f"]).abs()
    print("%s %s %s: cached=%d fresh=%d  index-mismatches=%d  max|close diff|=%.10f"
          % (args.symbol, args.timeframe, m, len(cached), len(fresh), missing,
             float(diff.max()) if len(diff) else 0.0))
    ok = missing == 0 and (not len(diff) or float(diff.max()) < 1e-9)
    print("VERIFY %s" % ("OK" if ok else "MISMATCH -- cached results are stale"))
    return 0 if ok else 2


def main(argv=None):
    p = argparse.ArgumentParser(prog="backend.data.snapshot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol")
    p.add_argument("--timeframe", default="M5")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--spec-only", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="refetch months already cached")
    p.add_argument("--list", action="store_true", help="show cache coverage")
    p.add_argument("--verify", metavar="YYYY-MM",
                   help="refetch one month and diff against the cache")
    args = p.parse_args(argv)

    if args.list:
        return cmd_list(args)
    if not args.symbol:
        p.error("--symbol is required (or use --list)")
    if args.verify:
        return cmd_verify(args)
    return cmd_snapshot(args)


def _cli():
    """Entry point: report expected failures cleanly instead of as a traceback."""
    from backend.core.errors import BotError
    try:
        return main()
    except BotError as exc:
        print('\n' + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(_cli())
