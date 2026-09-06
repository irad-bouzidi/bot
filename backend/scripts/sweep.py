"""Parameter sweep, train/test split and anchored walk-forward.

`Trading Bot.md` sections 11 and 16 ask for train/test separation, walk-forward,
out-of-sample validation and parameter-sensitivity analysis. None of it existed:
`run_baseline.py` runs ONE configuration, and every performance claim in this
repo came from a single in-sample run. This is that missing harness.

Three design decisions worth knowing before reading the code.

**It never shells out to `run_baseline`.** It imports `BacktestEngine` and
`compute_metrics` directly. Going through the CLI would write a ledger, a
metrics file and a config file per cell -- roughly a megabyte each, thousands of
times -- to recover numbers this process already holds in memory. One tidy CSV
row per cell instead.

**The geometry grid is in PIP COUNTS, shared across symbols.** `sl_pips`,
`tp_pips` and `be_trigger_pips` are swept as one set applied to every symbol and
multiplied through each symbol's own `pip` by `price_levels()`. A cell that gave
two symbols different counts is not expressible -- the "one rule, two
instruments" constraint made structural instead of a convention someone has to
remember.

**Ranking is on the NEIGHBOURHOOD, not the cell.** A configuration that beats
its neighbours threefold is the `RSI = 53` case section 11 warns about, not a
discovery. Every row carries `nbhd_median` / `nbhd_std` / `spike_ratio`, ranking
defaults to `nbhd_median`, and the objective is a t-statistic
(`expectancy_r / expectancy_r_se`) so a spectacular result on twelve trades
cannot win.
"""

import argparse
import itertools
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from backend.backtest.costs import CostConfig, CostModel, triple_weekday
from backend.backtest.engine import BacktestConfig, BacktestEngine
from backend.core.symbols import SYMBOL_CONFIG, price_levels
from backend.data.cache import DEFAULT_ROOT, CachedMarketData
from backend.data.market_data import BarSet
from backend.strategy.nw_envelope import NWConfig, NWEnvelopeStrategy

SCENARIOS = {
    "optimistic": (0.5, 0.0),
    "central": (1.0, 1.0),
    "stress": (2.0, 2.0),
}

# Swept in pip COUNTS and shared by every symbol; the rest are plain NWConfig
# fields. The list is explicit so an unknown --param is an error rather than a
# silently ignored typo that costs an hour of compute.
PIP_KEYS = ("sl_pips", "tp_pips", "be_trigger_pips")
STRATEGY_KEYS = ("bandwidth", "mult", "window", "mae_window", "entry_mode",
                 "exit_at_mean", "partial_fraction", "min_strength",
                 "max_spread_points", "atr_period")
GRID_KEYS = PIP_KEYS + STRATEGY_KEYS

DEFAULT_GRID = {
    "sl_pips": [70],
    "tp_pips": [100],
    "be_trigger_pips": [50],
    "entry_mode": ["level"],
    "exit_at_mean": [False],
    "partial_fraction": [0.5],
}

FIXED_COLS = ("symbol", "timeframe", "scenario", "split")


class BarCache(object):
    """One `BarSet` per (symbol, timeframe, span).

    Loading is ~0.3 s and every cell in a column shares it, which is the
    difference between minutes and an hour on a large grid.
    """

    def __init__(self, root):
        self.md = CachedMarketData(root=root, offline=True)
        self._cache = {}

    def get(self, symbol, timeframe, start, end, warmup):
        key = (symbol, timeframe, start, end, warmup)
        if key not in self._cache:
            self._cache[key] = self.md.get_bars(
                symbol, timeframe, start, end, warmup_bars=warmup)
        return self._cache[key]


def slice_barset(bs, lo, hi):
    """Evaluation rows [lo, hi), carrying their warm-up with them.

    A fold is expressed by MOVING the warm-up marker, not by trimming: the slice
    keeps every bar the indicators need before `lo` and evaluates only what
    follows. `nw_endpoint` is a causal convolution and `atr` a causal rolling
    mean, so a slice carrying at least `warmup_count` prior bars produces
    identical features to the full run -- which is the property that makes fold
    results comparable to each other at all.
    """
    w = bs.warmup_count
    abs_lo, abs_hi = w + lo, min(len(bs.df), w + hi)
    keep_from = max(0, abs_lo - w)
    return BarSet(df=bs.df.iloc[keep_from:abs_hi], spec=bs.spec, symbol=bs.symbol,
                  timeframe=bs.timeframe, warmup_count=abs_lo - keep_from,
                  source=bs.source, fetched_at=bs.fetched_at,
                  warnings=list(bs.warnings))


def build_config(cell, symbol):
    """One grid cell -> an `NWConfig` for `symbol`.

    The pip counts are shared; `price_levels()` is the single place they become
    price distances, so a symbol whose pip is not 0.1 cannot pick up gold's
    arithmetic by passing through here.
    """
    pip = price_levels(symbol)["pip"]
    kw = dict(sl_mode="fixed", sl_price=cell["sl_pips"] * pip,
              tp_mode="fixed", tp_price=cell["tp_pips"] * pip)
    for k in STRATEGY_KEYS:
        if k in cell:
            kw[k] = cell[k]
    be = cell.get("be_trigger_pips", 0) * pip
    if be > 0 and cell.get("partial_fraction", 0.0) > 0:
        kw["be_trigger_mode"] = "fixed"
        kw["be_trigger_price"] = be
    else:
        kw["be_trigger_mode"] = "none"
    return NWConfig(**kw)


def run_cell(bs, cell, symbol, scenario, balance, volume):
    spec = bs.spec
    spread_mult, slip_mult = SCENARIOS[scenario]
    costs = CostModel(CostConfig(
        spread_source="bar", spread_multiplier=spread_mult,
        # One typical spread of adverse fill on a stop, scaled by the scenario.
        # It cannot be a shared constant in POINTS: a point is 0.001 of gold and
        # 0.01 of Bitcoin.
        slippage_points_stop=float(spec.typical_spread_points or 0.0) * slip_mult,
        swap_long_points_per_day=spec.swap_long,
        swap_short_points_per_day=spec.swap_short,
        triple_swap_weekday=triple_weekday(spec.swap_rollover_3days),
    ), spec)
    eng = BacktestEngine(
        NWEnvelopeStrategy(build_config(cell, symbol)), spec, costs=costs,
        cfg=BacktestConfig(initial_balance=balance, volume=volume,
                           pip_size=price_levels(symbol)["pip"]))
    return eng.run(bs)


def row_from(res, cell, symbol, timeframe, scenario, split):
    m = res.metrics
    row = {"symbol": symbol, "timeframe": timeframe, "scenario": scenario,
           "split": split}
    row.update({k: cell[k] for k in GRID_KEYS if k in cell})
    for k, v in m.items():
        if not isinstance(v, dict):
            row[k] = v
    counts = m.get("exit_reason_counts", {}) or {}
    for reason in ("sl", "tp", "be_stop", "cross_center", "end_of_data",
                   "risk_halt", "signal"):
        row["exits_" + reason] = int(counts.get(reason, 0))
    meta = res.data_meta or {}
    row["bars_evaluated"] = meta.get("bars_evaluated")
    row["window_start"] = str(meta.get("start"))
    row["window_end"] = str(meta.get("end"))
    return row


def objective(row):
    """t-statistic of expectancy in R.

    Scale-free, sample-size aware, and it answers the only question a sample
    this size can answer: is the edge distinguishable from zero. Ranking on
    `total_pl` or on raw `expectancy_r` instead rewards whichever cell happened
    to take the fewest, luckiest trades.
    """
    e, se = row.get("expectancy_r"), row.get("expectancy_r_se")
    if e is None or se is None or not np.isfinite(se) or se <= 0:
        return float("nan")
    return float(e) / float(se)


def add_neighbourhood(df, swept):
    """Score every cell against its +/-1 neighbours on each numeric swept axis.

    Categorical axes (`entry_mode`, and the symbol/timeframe/scenario columns)
    have no ordering, so they are held FIXED rather than treated as neighbours.
    Averaging across them would hide exactly the difference being measured.
    """
    numeric = [k for k in swept
               if pd.api.types.is_numeric_dtype(df[k]) and df[k].nunique() > 1
               and not pd.api.types.is_bool_dtype(df[k])]
    fixed = [c for c in FIXED_COLS if c in df.columns] + \
            [k for k in swept if k not in numeric]
    levels = {k: sorted(df[k].dropna().unique().tolist()) for k in numeric}
    lookup = {}
    for _, r in df.iterrows():
        lookup[tuple(r[c] for c in fixed + numeric)] = r["objective"]

    med, mn, sd, nb, spike = [], [], [], [], []
    for _, r in df.iterrows():
        vals = [r["objective"]]
        base = [r[c] for c in fixed] + [r[c] for c in numeric]
        for k in numeric:
            pos = levels[k].index(r[k])
            for step in (-1, 1):
                j = pos + step
                if 0 <= j < len(levels[k]):
                    key = list(base)
                    key[len(fixed) + numeric.index(k)] = levels[k][j]
                    v = lookup.get(tuple(key))
                    if v is not None:
                        vals.append(v)
        arr = np.array([v for v in vals if v is not None and np.isfinite(v)],
                       dtype=float)
        if not len(arr):
            med.append(np.nan), mn.append(np.nan), sd.append(np.nan)
            nb.append(0), spike.append(np.nan)
            continue
        m = float(np.median(arr))
        med.append(m)
        mn.append(float(arr.min()))
        sd.append(float(arr.std(ddof=0)))
        nb.append(len(arr) - 1)
        spike.append(float(r["objective"] / m) if m else np.nan)
    df["nbhd_median"], df["nbhd_min"], df["nbhd_std"] = med, mn, sd
    df["n_neighbours"], df["spike_ratio"] = nb, spike
    df["plateau_score"] = df["nbhd_median"] - df["nbhd_std"]
    return df


def _coerce(v):
    v = v.strip()
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def parse_grid(args):
    grid = dict(DEFAULT_GRID)
    if args.grid:
        with open(args.grid) as fh:
            grid.update(json.load(fh))
    for spec in args.param or []:
        if "=" not in spec:
            raise SystemExit("--param must be key=v1,v2,... (got %r)" % spec)
        key, values = spec.split("=", 1)
        key = key.strip()
        if key not in GRID_KEYS:
            raise SystemExit("unknown sweep parameter %r. Known: %s"
                             % (key, ", ".join(GRID_KEYS)))
        grid[key] = [_coerce(v) for v in values.split(",")]
    unknown = set(grid) - set(GRID_KEYS)
    if unknown:
        raise SystemExit("unknown sweep parameter(s): %s" % ", ".join(sorted(unknown)))
    return grid


def cells_of(grid):
    keys = [k for k in GRID_KEYS if k in grid]
    for combo in itertools.product(*[grid[k] for k in keys]):
        yield dict(zip(keys, combo))


def _utc(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       stderr=subprocess.DEVNULL).decode().strip()
    except Exception:                                          # noqa: BLE001
        return "unknown"


def report(df, args, swept):
    train = df[df.split.isin(("train", "full"))]
    gate = train.closed_trades >= args.min_trades
    gated = train[gate]
    print("\ncells run: %d   passing the >=%d closed-trade gate: %d"
          % (len(train), args.min_trades, len(gated)))
    if not len(gated):
        print("nothing passed -- widen the window or lower --min-trades")
        return None
    if swept:
        gated = gated[gated.n_neighbours >= args.min_neighbours]
        if not len(gated):
            print("nothing had >=%d neighbours -- the grid is too small to "
                  "distinguish a plateau from a spike" % args.min_neighbours)
            return None
    keys = [c for c in FIXED_COLS if c in gated.columns] + list(swept)
    # `split` identifies the fold, so it must NOT join train rows to their
    # held-out twins -- merging on it can only ever produce an empty frame.
    join_keys = [c for c in keys if c != "split"]
    show = keys + [c for c in ("closed_trades", "win_rate", "total_pl",
                               "profit_factor", "expectancy_r", "objective",
                               "nbhd_median", "spike_ratio", "max_drawdown")
                   if c in gated.columns]
    rank = "nbhd_median" if swept and gated.nbhd_median.notna().any() else "objective"
    best = gated.sort_values(rank, ascending=False).head(args.top)
    print("\n--- top %d by %s ---" % (args.top, rank))
    print("(spike_ratio far from 1.0 means the cell beat its own neighbours: a "
          "lucky cell, not a plateau)")
    print(best[show].to_string(index=False, float_format=lambda v: "%.3f" % v))

    held = df[df.split == "test"]
    if len(held):
        print("\n--- the same configurations on the HELD-OUT window ---")
        print("(selection never saw these bars; this is the only unselected "
              "number in the run)")
        merged = held.merge(best[join_keys].head(5), on=join_keys, how="inner")
        print(merged[show].to_string(index=False, float_format=lambda v: "%.3f" % v)
              if len(merged) else "(no matching held-out rows)")
    return best


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--symbols", default=",".join(sorted(SYMBOL_CONFIG)))
    p.add_argument("--timeframes", default="M5")
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--volume", type=float, default=0.1)
    p.add_argument("--scenarios", default="central")
    p.add_argument("--grid", default=None, help="JSON file of {param: [values]}")
    p.add_argument("--param", action="append",
                   help="key=v1,v2,... (repeatable). Geometry keys are PIP "
                        "COUNTS and are shared by every symbol.")
    p.add_argument("--holdout", type=float, default=0.3,
                   help="fraction of the window reserved BY TIME and never used "
                        "for selection. 0 disables.")
    p.add_argument("--min-trades", type=int, default=100)
    p.add_argument("--min-neighbours", type=int, default=2)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)

    grid = parse_grid(args)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    for s in symbols:
        if s not in SYMBOL_CONFIG:
            raise SystemExit("unconfigured symbol %r" % s)
    for s in scenarios:
        if s not in SCENARIOS:
            raise SystemExit("unknown scenario %r" % s)

    cells = list(cells_of(grid))
    swept = [k for k in GRID_KEYS if k in grid and len(grid[k]) > 1]
    print("grid: %d cells x %d symbols x %d timeframes x %d scenarios"
          % (len(cells), len(symbols), len(timeframes), len(scenarios)))
    print("swept axes: %s" % (", ".join(swept) or "(none -- a single point)"))

    cache = BarCache(args.root)
    start, end = _utc(args.start), _utc(args.end)
    rows, done = [], 0
    for symbol in symbols:
        for tf in timeframes:
            warmup = NWEnvelopeStrategy(build_config(cells[0], symbol)).warmup_bars()
            try:
                full = cache.get(symbol, tf, start, end, warmup)
            except Exception as exc:                           # noqa: BLE001
                print("SKIP %s %s: %s" % (symbol, tf, str(exc).split("\n")[0]))
                continue
            n_eval = len(full.df) - full.warmup_count
            splits = [("full", 0, n_eval)]
            if args.holdout > 0:
                cut = int(n_eval * (1.0 - args.holdout))
                splits = [("train", 0, cut), ("test", cut, n_eval)]
            for split, lo, hi in splits:
                if hi - lo < 500:
                    print("SKIP %s %s %s: only %d evaluable bars"
                          % (symbol, tf, split, hi - lo))
                    continue
                sub = full if split == "full" else slice_barset(full, lo, hi)
                for cell in cells:
                    for sc in scenarios:
                        res = run_cell(sub, cell, symbol, sc, args.balance,
                                       args.volume)
                        rows.append(row_from(res, cell, symbol, tf, sc, split))
                        done += 1
                        if done % 50 == 0:
                            print("  ... %d runs" % done)

    if not rows:
        raise SystemExit("no cells ran -- check --symbols/--timeframes "
                         "against what is in data/bars/")

    df = pd.DataFrame(rows)
    df["objective"] = df.apply(objective, axis=1)
    if swept:
        df = add_neighbourhood(df, swept)
    else:
        for col in ("nbhd_median", "nbhd_min", "nbhd_std", "spike_ratio",
                    "plateau_score"):
            df[col] = np.nan
        df["n_neighbours"] = 0

    out = args.out or os.path.join(args.root, "sweeps",
                                   datetime.now().strftime("%Y%m%d_%H%M%S"))
    if not os.path.isdir(out):
        os.makedirs(out)
    df.to_csv(os.path.join(out, "cells.csv"), index=False)
    with open(os.path.join(out, "config.json"), "w") as fh:
        json.dump({"argv": sys.argv[1:], "grid": grid, "swept": swept,
                   "git_sha": _git_sha(),
                   "created_at": datetime.now(timezone.utc).isoformat()},
                  fh, indent=2, default=str)

    report(df, args, swept)
    print("\nwrote %d rows to %s" % (len(df), os.path.join(out, "cells.csv")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
