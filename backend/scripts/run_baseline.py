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

Every scenario also reports PIPS won and lost, from the symbol's own pip in
SYMBOL_CONFIG. They are gross price distance -- a distance cannot carry a
spread -- so read them against the money figures rather than as a version of
them: the difference between the two is what the cost scenario charged. Each
trade is measured entry to FINAL exit as though the lot were 0.01, so `--volume`
and the scale-out move every money figure in the report and none of the pips
ones. A run whose scale-out banks half at +50 and scratches its runner shows
that trade as 0 pips and a positive P&L; both are true of it.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

from backend.backtest.costs import CostConfig, CostModel, triple_weekday
from backend.backtest.engine import BacktestConfig, BacktestEngine
from backend.backtest.risk import RiskConfig
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
        exit_at_mean=bool(args.exit_at_mean),
    ))


def run_one(barset, args, spread_mult, slip_mult, legacy=False):
    spec = barset.spec
    # A stop is hit precisely during a fast adverse move, so it fills worse than
    # an average order -- see costs.py's module docstring. Until the entry bar
    # was resolved, the engine supplied that pessimism by accident (a rule-5 gap
    # fill averaging 3.0 past a 7.00 gold stop, i.e. ~30 pips). Now it has to be
    # a number somebody chose. It cannot be a shared constant in POINTS: one
    # point is 0.001 of gold and 0.01 of Bitcoin, so it defaults to one typical
    # spread of the instrument and scales with the scenario like every other
    # cost.
    stop_slip = (args.slippage if args.slippage_stop is None
                 else args.slippage_stop)
    costs = CostModel(CostConfig(
        spread_source="none" if args.no_costs else "bar",
        spread_multiplier=spread_mult,
        commission_per_lot_round_turn=args.commission,
        slippage_points_entry=args.slippage * slip_mult,
        slippage_points_exit=args.slippage * slip_mult,
        slippage_points_stop=stop_slip * slip_mult,
        swap_long_points_per_day=0.0 if args.no_swap else spec.swap_long,
        swap_short_points_per_day=0.0 if args.no_swap else spec.swap_short,
        triple_swap_weekday=triple_weekday(spec.swap_rollover_3days),
    ), spec)
    eng = BacktestEngine(
        build_strategy(args), spec, costs=costs,
        cfg=BacktestConfig(
            initial_balance=args.balance, volume=args.volume,
            legacy_mode=legacy, pip_size=args.pip_size,
            risk=RiskConfig(
                risk_pct_per_trade=args.risk_pct,
                max_daily_loss_pct=args.max_daily_loss_pct,
                max_consecutive_losses=args.max_consecutive_losses,
                cooldown_bars=args.cooldown_bars,
                min_equity=args.min_equity)),
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
        # Pips are GROSS price distance and the money above is net of the cost
        # model, so these two blocks are meant to be read against each other:
        # the gap between "positive on pips" and "positive on P&L" is what the
        # spread and commission took. pips_lost prints negative, so net_pips is
        # the two added.
        ("Pips won", "pips_won"), ("Pips lost", "pips_lost"),
        ("Net pips", "net_pips"),
        ("  pip wins / losses", None),
        ("Avg win (pips)", "avg_win_pips"), ("Avg loss (pips)", "avg_loss_pips"),
        ("Largest win (pips)", "largest_win_pips"),
        ("Largest loss (pips)", "largest_loss_pips"),
        ("Max drawdown %", "max_drawdown"),
        ("Min equity seen", "min_equity_seen"),
        ("HALTED", "halted"), ("  reason", "halt_reason"),
        ("Entries blocked: daily loss", "entries_blocked_daily_loss"),
        ("  cooldown", "entries_blocked_cooldown"),
        ("  after halt", "entries_blocked_halted"),
        ("  size below broker minimum", "entries_skipped_too_small"),
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
    pairs = {
        "Wins / Losses": ("wins", "losses"),
        # Its own pair, and not a restatement of the one above: a trade decided
        # by the spread lands in a different bucket in each.
        "  pip wins / losses": ("pip_wins", "pip_losses"),
    }
    for label, key in rows:
        if key is None:
            a, b = pairs[label]
            val = "%s / %s" % (m.get(a), m.get(b))
        else:
            val = g(key)
        print("  %-22s %s" % (label, val))
    print("  %-22s %s" % ("Exit reasons", m.get("exit_reason_counts")))
    for note in ("sharpe_note", "sample_note", "max_consecutive_losses_note"):
        if m.get(note):
            print("  NOTE: %s" % m[note])


def _apply_symbol_defaults(args):
    """Resolve the settings that have per-symbol defaults, and SAY WHAT THEY ARE.

    --sl / --tp / --be-trigger-fraction and the centre-line exit, all from
    SYMBOL_CONFIG. Same reason for each: the live bot holds its geometry in pips
    (70 x 0.1 on gold, 700 x 1.0 on Bitcoin) and this script takes price units,
    so the conversion has to happen somewhere; doing it here means the two
    cannot drift, and printing it means a report can be read six months later
    without guessing which numbers produced it. An unconfigured symbol must pass
    the geometry explicitly rather than inherit another instrument's stop.
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

    # Resolved here for the same reason --sl/--tp are: the pip DEFINITION lives
    # in SYMBOL_CONFIG (gold 0.1, Bitcoin 10.0 -- $1 and $100 respectively are
    # 10 pips) and the engine deliberately does not import it, so that
    # backend/backtest/ stays runnable with nothing but `data/`. Unlike the stop
    # distances this is not a SystemExit for an unknown symbol: it decides how a
    # result is REPORTED, never how it is traded, so 0.0 -- "no pips in this
    # report" -- is a safe answer where guessing a stop distance would not be.
    args.pip_size = float(SYMBOL_CONFIG.get(args.symbol, {}).get("pip", 0.0) or 0.0)
    print("pips: %s"
          % ("1 pip = %g of price (SYMBOL_CONFIG[%s])" % (args.pip_size, args.symbol)
             if args.pip_size else
             "not reported -- %s has no pip in SYMBOL_CONFIG" % args.symbol))

    # Not part of the loop above: this one is a flag, not a price level, and an
    # unconfigured symbol gets a usable default rather than a SystemExit. FALSE
    # is safe to assume where a stop distance is not -- it only removes an exit,
    # and it is what every configured symbol ships.
    if args.exit_at_mean is None:
        args.exit_at_mean = bool(
            SYMBOL_CONFIG.get(args.symbol, {}).get("exit_at_mean", False))
        source = "SYMBOL_CONFIG[%s]" % args.symbol if known else "the default"
    else:
        source = "the command line"
    # Printed on every run, on or off, for the same reason the geometry is: this
    # rule changes which trades exist, so a report that does not say whether it
    # was on cannot be compared with one that does. Reports stored before the
    # flag existed were all produced with it ON.
    print("centre-line exit: %s (from %s)"
          % ("ON -- a scaled-out runner is usually closed before the target"
             if args.exit_at_mean else
             "OFF -- stop, break-even or target only", source))



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
    p.add_argument("--slippage", type=float, default=0.0,
                   help="points, applied to entries and non-stop exits")
    p.add_argument("--slippage-stop", type=float, default=None,
                   help="EXTRA points on SL/break-even exits only. Defaults to "
                        "one typical spread of the instrument; pass 0 to model "
                        "stops filling exactly at their level.")
    p.add_argument("--no-swap", action="store_true",
                   help="ignore the broker's swap rates, to measure them")
    p.add_argument("--no-costs", action="store_true")
    p.add_argument("--no-breakeven", action="store_true",
                   help="disable the scale-out / break-even rule, to measure it")
    # Tri-state on purpose: None means "take the symbol's shipped value", so the
    # research run models the live bot without being told to. --no-breakeven
    # above is a plain store_true because the scale-out ships ON everywhere; this
    # rule ships OFF, so a store_true alone could only ever turn it on and there
    # would be no way to say "on for this symbol, off for that one".
    p.add_argument("--exit-at-mean", dest="exit_at_mean",
                   action="store_true", default=None,
                   help="close on a return to the envelope centre line "
                        "(default: the symbol's SYMBOL_CONFIG exit_at_mean)")
    p.add_argument("--no-exit-at-mean", dest="exit_at_mean",
                   action="store_false",
                   help="leave a position to its stop, break-even or target only")
    p.add_argument("--be-trigger-fraction", type=float, default=None,
                   help="scale-out trigger as a fraction of the TP distance "
                        "(default: the symbol's be_trigger_pips / tp_pips)")
    p.add_argument("--partial-fraction", type=float, default=0.5,
                   help="proportion of the position closed at the trigger")
    p.add_argument("--compare-legacy", action="store_true",
                   help="also run the ORIGINAL close-only, cost-free engine to show "
                        "how much it was flattering itself")
    p.add_argument("--risk-pct", type=float, default=0.0,
                   help="PERCENT of equity risked at the stop, e.g. 1.0 for 1%%. "
                        "0 keeps the fixed --volume, which is what every stored "
                        "report used.")
    p.add_argument("--max-daily-loss-pct", type=float, default=0.0,
                   help="percent of the broker day's OPENING equity; blocks new "
                        "entries for the rest of that day")
    p.add_argument("--max-consecutive-losses", type=int, default=0)
    p.add_argument("--cooldown-bars", type=int, default=0,
                   help="bars to sit out after --max-consecutive-losses")
    p.add_argument("--min-equity", type=float, default=0.0,
                   help="absolute equity floor; breaching it STOPS the run, so "
                        "drawdown stays a readable percentage")
    p.add_argument("--out", default=None, help="directory for ledger + metrics")
    args = p.parse_args(argv)
    _apply_symbol_defaults(args)

    md = CachedMarketData(root=args.root, offline=True)
    strat = build_strategy(args)
    barset = md.get_bars(args.symbol, args.timeframe, _utc(args.start), _utc(args.end),
                         warmup_bars=strat.warmup_bars())

    spec = barset.spec
    if args.slippage_stop is None:
        # One typical spread of THIS instrument. See run_one for why it cannot
        # be a shared constant in points.
        args.slippage_stop = float(spec.typical_spread_points or 0.0)
    if spec.swap_mode not in (0, 1) and not args.no_swap:
        raise SystemExit(
            "%s reports swap_mode=%d; CostModel only implements POINTS (mode 1). "
            "Charging a percentage as though it were points would be wrong by "
            "orders of magnitude. Re-run with --no-swap to proceed deliberately "
            "without it." % (spec.name, spec.swap_mode))
    print("costs: stop slippage %g pt (%g of price)%s | swap %g/%g pt per day, "
          "triple on %s%s"
          % (args.slippage_stop, args.slippage_stop * spec.point,
             " [default: one typical spread]" if args.slippage_stop else "",
             0.0 if args.no_swap else spec.swap_long,
             0.0 if args.no_swap else spec.swap_short,
             ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[
                 triple_weekday(spec.swap_rollover_3days)],
             " (--no-swap)" if args.no_swap else ""))
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
    # `config_used` in its OWN file rather than folded into the metrics json,
    # whose top level is a scenario map -- adding a sibling key there would read
    # as a fourth scenario to anything already iterating it. It is written at
    # all because the engine has always assembled it and nothing ever saved it:
    # a stored report could not say what produced it, which the centre-line
    # exit flag makes material: two runs that differ only in a rule that changes
    # which trades exist would otherwise be indistinguishable on disk.
    with open(os.path.join(out, stamp + "_config.json"), "w") as fh:
        json.dump({k: v.config_used for k, v in results.items()}, fh,
                  indent=2, default=str)
    print("\nwrote %s_ledger.csv, %s_metrics.json and %s_config.json to %s"
          % (stamp, stamp, stamp, out))
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
