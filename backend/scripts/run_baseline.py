"""Produce the baseline performance report from cached bars.

Runs entirely offline against `data/` -- no MT5 needed. Snapshot first on the
trading host (see backend/data/snapshot.py), copy `data/` here, then:

    python -m backend.scripts.run_baseline --symbol XAUUSDm
    python -m backend.scripts.run_baseline --symbol BTCUSDm
    python -m backend.scripts.run_baseline --symbol XAUUSDm --compare-legacy

Reports every metric the spec asks for, plus breakdowns by session, day of week,
direction and exit reason, and writes the full trade ledger so the diagnosis
phase has something to work with.

ONE SYMBOL PER RUN, on purpose. The dashboard's /backtest can replay several
symbols onto one account; this script cannot, because a report here is the basis
for a decision about a strategy on an instrument and averaging two instruments'
edges together is how a losing one hides behind a winning one. Run it twice.

--sl / --tp are PRICE units and default to the symbol's own SYMBOL_CONFIG
geometry (gold 7/10, Bitcoin 700/1000), printed at the top of every report so a
saved run says what produced it.

Three cost scenarios are always reported. The CENTRAL column is the decision
basis: entries fire during volatility expansions, when spreads are widest, so a
median spread understates what this strategy actually pays.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

from backend.backtest.costs import CostConfig, CostModel
from backend.backtest.engine import BacktestConfig, BacktestEngine
from backend.backtest.metrics import by_group
from backend.core.symbols import SYMBOL_CONFIG, price_levels
from backend.data.cache import DEFAULT_ROOT, CachedMarketData
from backend.strategy.nw_envelope import NWConfig, NWEnvelopeStrategy

SCENARIOS = [
    ("optimistic", 0.5, 0.0),
    ("central", 1.0, 1.0),
    ("stress", 2.0, 2.0),
]


def _utc(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def build_strategy(args):
    return NWEnvelopeStrategy(NWConfig(
        bandwidth=args.bandwidth, mult=args.mult, window=args.window,
        mae_window=args.mae_window, entry_mode=args.entry_mode,
        sl_mode="fixed", sl_price=args.sl, tp_mode="fixed", tp_price=args.tp,
        be_trigger_mode="none" if args.no_breakeven else "tp_fraction",
        be_trigger_tp_fraction=args.be_trigger_fraction,
        partial_fraction=args.partial_fraction,
    ))


def run_one(barset, args, spread_mult, slip_mult, legacy=False):
    spec = barset.spec
    costs = CostModel(CostConfig(
        spread_source="none" if args.no_costs else "bar",
        spread_multiplier=spread_mult,
        commission_per_lot_round_turn=args.commission,
        slippage_points_entry=args.slippage * slip_mult,
        slippage_points_exit=args.slippage * slip_mult,
        slippage_points_stop=args.slippage * slip_mult,
    ), spec)
    eng = BacktestEngine(
        build_strategy(args), spec, costs=costs,
        cfg=BacktestConfig(initial_balance=args.balance, volume=args.volume,
                           legacy_mode=legacy),
    )
    return eng.run(barset)


def fmt(m):
    def g(k, d="-"):
        v = m.get(k)
        if v is None:
            return d
        if isinstance(v, float):
            return "inf" if v == float("inf") else "%.2f" % v
        return str(v)
    return g


def print_report(name, m):
    g = fmt(m)
    print("\n" + "=" * 68)
    print("  %s" % name)
    print("=" * 68)
    rows = [
        ("Trades opened", "trades_opened"), ("Closed trades", "closed_trades"),
        ("Wins / Losses", None), ("Win rate %", "win_rate"),
        ("Gross profit", "gross_profit"), ("Gross loss", "gross_loss"),
        ("Net P&L", "total_pl"), ("Profit factor", "profit_factor"),
        ("Expectancy / trade", "expectancy"), ("Expectancy (R)", "expectancy_r"),
        ("Avg win", "avg_win"), ("Avg loss", "avg_loss"),
        ("Realized R:R", "realized_rr"),
        ("Largest win", "largest_win"), ("Largest loss", "largest_loss"),
        ("Max drawdown %", "max_drawdown"),
        ("Max consec. wins", "max_consecutive_wins"),
        ("Max consec. losses", "max_consecutive_losses"),
        ("  (iid expectation)", "max_consecutive_losses_expected_iid"),
        ("ROI %", "roi_pct"), ("Sharpe", "sharpe"), ("Sortino", "sortino"),
        ("Avg bars held", "avg_bars_held"), ("Trades / day", "trades_per_day"),
        ("Scale-outs fired", "partials_fired"), ("  as % of trades", "partials_fired_pct"),
        ("  banked on partials", "partial_pl"),
        ("Long win rate %", "long_win_rate"), ("Short win rate %", "short_win_rate"),
        ("Long P&L", "long_pl"), ("Short P&L", "short_pl"),
    ]
    for label, key in rows:
        val = "%s / %s" % (m.get("wins"), m.get("losses")) if key is None else g(key)
        print("  %-22s %s" % (label, val))
    print("  %-22s %s" % ("Exit reasons", m.get("exit_reason_counts")))
    for note in ("sharpe_note", "sample_note", "max_consecutive_losses_note"):
        if m.get(note):
            print("  NOTE: %s" % m[note])


def _apply_symbol_defaults(args):
    """Fill --sl / --tp / --be-trigger-fraction from SYMBOL_CONFIG, and SAY SO.

    The live bot holds its geometry in pips (70 x 0.1 on gold, 700 x 1.0 on
    Bitcoin) and this script takes price units, so the conversion has to happen
    somewhere; doing it here means the two cannot drift, and printing it means a
    report can be read six months later without guessing which numbers produced
    it. An unconfigured symbol must pass both explicitly rather than inherit
    another instrument's stop.
    """
    known = args.symbol in SYMBOL_CONFIG
    levels = price_levels(args.symbol) if known else None
    chosen = []
    for flag, key in (("sl", "sl_price"), ("tp", "tp_price"),
                      ("be_trigger_fraction", "be_trigger_tp_fraction")):
        if getattr(args, flag) is not None:
            continue
        if not known:
            raise SystemExit(
                "%s is not in SYMBOL_CONFIG, so --%s has no default. Pass --sl "
                "and --tp explicitly (PRICE units), or add the symbol to "
                "backend/core/symbols.py." % (args.symbol, flag.replace("_", "-")))
        setattr(args, flag, levels[key])
        chosen.append("%s=%g" % (flag, levels[key]))
    if chosen:
        print("geometry: %s from SYMBOL_CONFIG[%s] (%s)"
              % (", ".join(chosen), args.symbol,
                 "pip=%g sl=%g tp=%g pips" % (levels["pip"],
                                              SYMBOL_CONFIG[args.symbol]["sl_pips"],
                                              SYMBOL_CONFIG[args.symbol]["tp_pips"])))


def main(argv=None):
    p = argparse.ArgumentParser(prog="backend.scripts.run_baseline",
                                description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", required=True)
    p.add_argument("--timeframe", default="M5")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--volume", type=float, default=0.1)
    p.add_argument("--bandwidth", type=float, default=8.0)
    p.add_argument("--mult", type=float, default=3.0)
    p.add_argument("--window", type=int, default=500)
    p.add_argument("--mae-window", type=int, default=500)
    p.add_argument("--entry-mode", default="level", choices=["level", "cross"])
    # No numeric default: 7.0/10.0 is gold's geometry in price units, and
    # silently applying it to BTCUSDm would put a $7 stop on an $81,000
    # instrument and report the result as a baseline. Left None and resolved from
    # SYMBOL_CONFIG below, so the pip counts the live bot trades are the pip
    # counts this measures.
    p.add_argument("--sl", type=float, default=None,
                   help="stop distance, PRICE units (default: the symbol's "
                        "SYMBOL_CONFIG stop)")
    p.add_argument("--tp", type=float, default=None,
                   help="target distance, PRICE units (default: the symbol's "
                        "SYMBOL_CONFIG target)")
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--slippage", type=float, default=0.0, help="points")
    p.add_argument("--no-costs", action="store_true")
    p.add_argument("--no-breakeven", action="store_true",
                   help="disable the scale-out / break-even rule, to measure it")
    p.add_argument("--be-trigger-fraction", type=float, default=None,
                   help="scale-out trigger as a fraction of the TP distance "
                        "(default: the symbol's be_trigger_pips / tp_pips)")
    p.add_argument("--partial-fraction", type=float, default=0.5,
                   help="proportion of the position closed at the trigger")
    p.add_argument("--compare-legacy", action="store_true",
                   help="also run the ORIGINAL close-only, cost-free engine to show "
                        "how much it was flattering itself")
    p.add_argument("--out", default=None, help="directory for ledger + metrics")
    args = p.parse_args(argv)
    _apply_symbol_defaults(args)

    md = CachedMarketData(root=args.root, offline=True)
    strat = build_strategy(args)
    barset = md.get_bars(args.symbol, args.timeframe, _utc(args.start), _utc(args.end),
                         warmup_bars=strat.warmup_bars())

    print("data: %s" % json.dumps(barset.meta(), indent=2, default=str))
    for w in barset.warnings:
        print("WARNING: %s" % w)

    results = {}
    if args.compare_legacy:
        res = run_one(barset, args, 0.0, 0.0, legacy=True)
        print_report("ORIGINAL ENGINE (close-only exits, no costs) -- NOT TRUSTWORTHY",
                     res.metrics)
        results["legacy"] = res

    for name, sm, sl in SCENARIOS:
        res = run_one(barset, args, sm, sl)
        label = "%s COSTS%s" % (name.upper(), "  <-- DECISION BASIS"
                                if name == "central" else "")
        print_report(label, res.metrics)
        results[name] = res

    central = results["central"]
    for key in ("session", "day_of_week", "side", "exit_reason"):
        tbl = by_group(central.ledger, key)
        if len(tbl):
            print("\n--- central costs, by %s ---" % key)
            print(tbl.to_string())

    if args.compare_legacy and "legacy" in results:
        a = results["legacy"].metrics["total_pl"]
        b = central.metrics["total_pl"]
        print("\n" + "!" * 68)
        print("  Original engine reported : %+.2f" % a)
        print("  Honest engine reports    : %+.2f" % b)
        print("  Overstatement            : %+.2f" % (a - b))
        print("!" * 68)

    out = args.out or os.path.join(args.root, "reports")
    os.makedirs(out, exist_ok=True)
    stamp = "%s_%s" % (args.symbol, datetime.now().strftime("%Y%m%d_%H%M%S"))
    central.ledger.to_csv(os.path.join(out, stamp + "_ledger.csv"), index=False)
    with open(os.path.join(out, stamp + "_metrics.json"), "w") as fh:
        json.dump({k: v.metrics for k, v in results.items()}, fh,
                  indent=2, default=str)
    print("\nwrote %s_ledger.csv and %s_metrics.json to %s" % (stamp, stamp, out))
    return 0


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
