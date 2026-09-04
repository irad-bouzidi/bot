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
from backend.core.news import (DEFAULT_IMPACTS, empty_calendar, news_dir,
                               read_calendar)
from backend.core.symbols import (NEWS_AFTER_MINUTES, NEWS_BEFORE_MINUTES,
                                  SYMBOL_CONFIG, news_currencies_for,
                                  price_levels)
from backend.data.cache import DEFAULT_ROOT, CachedMarketData
from backend.strategy.nw_envelope import NWConfig, NWEnvelopeStrategy

SCENARIOS = [
    ("optimistic", 0.5, 0.0),
    ("central", 1.0, 1.0),
    ("stress", 2.0, 2.0),
]


def _utc(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def build_calendar(args):
    """Load the news calendar, or an empty one when --news-file is absent.

    Empty is a real answer here, not a fallback: it means "nothing was
    scheduled", so the run reproduces the pre-news-filter behaviour exactly.
    That is deliberately the OPPOSITE of the live path, which refuses to open a
    trade on a calendar it cannot vouch for -- there a real position is at
    stake, whereas failing closed in a backtest would silently delete trades and
    report the remainder as a result.
    """
    impacts = tuple(i.strip().lower() for i in args.news_impacts.split(",")
                    if i.strip())
    if args.news_currencies is None:
        currencies = news_currencies_for(args.symbol) or None
    elif args.news_currencies.strip().lower() in ("", "any", "all"):
        currencies = None
    else:
        currencies = tuple(c.strip().upper()
                           for c in args.news_currencies.split(",") if c.strip())
    kwargs = dict(before_minutes=args.news_before, after_minutes=args.news_after,
                  impacts=impacts, currencies=currencies)
    if not args.news_file:
        return empty_calendar(**kwargs), currencies, impacts
    return read_calendar(args.news_file, **kwargs), currencies, impacts


def build_strategy(args):
    calendar, currencies, impacts = build_calendar(args)
    return NWEnvelopeStrategy(NWConfig(
        bandwidth=args.bandwidth, mult=args.mult, window=args.window,
        mae_window=args.mae_window, entry_mode=args.entry_mode,
        sl_mode="fixed", sl_price=args.sl, tp_mode="fixed", tp_price=args.tp,
        be_trigger_mode="none" if args.no_breakeven else "tp_fraction",
        be_trigger_tp_fraction=args.be_trigger_fraction,
        partial_fraction=args.partial_fraction,
        news_enabled=bool(args.news_file),
        news_before_minutes=args.news_before,
        news_after_minutes=args.news_after,
        news_impacts=impacts,
        news_currencies=tuple(currencies or ()),
    ), calendar=calendar)


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
    """Resolve the settings that have per-symbol defaults, and SAY WHAT THEY ARE.

    --sl / --tp / --be-trigger-fraction from SYMBOL_CONFIG, plus the news
    blackout's state. Both for the same reason: the live bot holds its geometry
    in pips (70 x 0.1 on gold, 700 x 1.0 on Bitcoin) and this script takes price
    units, so the conversion has to happen somewhere; doing it here means the
    two cannot drift, and printing it means a report can be read six months
    later without guessing which numbers produced it. An unconfigured symbol
    must pass the geometry explicitly rather than inherit another instrument's
    stop.

    The news line is printed even when the filter is OFF, because a silent
    absence reads as "there was no such rule" -- and there now is one.
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

    # Printed for the same reason as the geometry above: the news filter changes
    # which trades exist, so a report that does not say whether it was on cannot
    # be compared with one that does. "off" is stated explicitly rather than
    # omitted -- a silent absence reads as "there was no such rule".
    calendar, currencies, impacts = build_calendar(args)
    if not args.news_file:
        print("news filter: OFF (no --news-file) -- comparable with reports "
              "made before the blackout rule existed")
    else:
        print("news filter: ON, -%g/+%gmin around %d events from %s "
              "(impacts=%s currencies=%s)"
              % (args.news_before, args.news_after, len(calendar),
                 args.news_file, ",".join(impacts),
                 "any" if currencies is None else ",".join(currencies)))
        if calendar.is_empty:
            print("  WARNING: the calendar matched 0 events, so this run is "
                  "identical to --news-file being absent. Check the impact and "
                  "currency filters and the date range of the file.")


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
    # News blackout. OFF unless --news-file is given: with no calendar there are
    # no events, and a run with no events must reproduce the pre-filter numbers
    # exactly so old reports stay comparable.
    p.add_argument("--news-file", default=None,
                   help="CSV file or directory of them (e.g. %s) holding the "
                        "economic calendar. Omit to disable the news blackout "
                        "entirely. Fetch one with: python -m "
                        "backend.live.news_feed --fetch"
                        % news_dir(DEFAULT_ROOT))
    p.add_argument("--news-before", type=float, default=NEWS_BEFORE_MINUTES,
                   help="minutes before an event to stop trading")
    p.add_argument("--news-after", type=float, default=NEWS_AFTER_MINUTES,
                   help="minutes after an event to resume trading")
    p.add_argument("--news-impacts", default=",".join(DEFAULT_IMPACTS),
                   help="comma-separated impact levels that trigger a blackout")
    p.add_argument("--news-currencies", default=None,
                   help="comma-separated currencies whose events count, or "
                        "'any' (default: the symbol's news_currencies)")
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
    # `config_used` in its OWN file rather than folded into the metrics json,
    # whose top level is a scenario map -- adding a sibling key there would read
    # as a fourth scenario to anything already iterating it. It is written at
    # all because the engine has always assembled it and nothing ever saved it:
    # a stored report could not say what produced it, which the news filter
    # makes material, since a filtered and an unfiltered run differ only in
    # settings that lived nowhere on disk.
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
