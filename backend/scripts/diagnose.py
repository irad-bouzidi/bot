"""Read one ledger and say where the money went.

`Trading Bot.md` sections 2, 3, 9 and 10 ask for a breakdown by session, day,
direction, regime and excursion. `run_baseline` prints four `by_group` tables;
this is the rest of it, and it runs against any stored ledger CSV, so a report
made months ago can still be interrogated without re-running anything.

The first table is the bars-held histogram, deliberately. A spike at
`bars_held == 1` is the fingerprint of the entry-bar blind spot -- the engine
used not to check SL/TP on the bar a trade filled on, so half of gold's trades
exited on the next bar's open at a rule-5 gap price, an average of 3.0 past a
7.00 stop. That bug survived a test file with one test per execution rule
because nothing ever looked at this distribution. Now something does, and it
prints a warning when one bucket dominates.
"""

import argparse
import os

import numpy as np
import pandas as pd

from backend.backtest.metrics import by_group

pd.set_option("display.width", 200)


def _f(v):
    return "%.3f" % v if isinstance(v, float) else str(v)


def _show(title, frame, note=None):
    if frame is None or not len(frame):
        return
    print("\n--- %s ---" % title)
    if note:
        print(note)
    print(frame.to_string(float_format=lambda v: "%.3f" % v))


def load(path):
    d = pd.read_csv(path)
    for col in ("entry_time", "exit_time"):
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], utc=True, errors="coerce")
    if "is_open" in d.columns:
        d = d[~d["is_open"].astype(bool)]
    if "bars_held" not in d.columns and {"entry_index", "exit_index"} <= set(d.columns):
        d["bars_held"] = d.exit_index - d.entry_index
    return d.reset_index(drop=True)


# --- 1. the histogram that would have caught the entry-bar bug --------------

def bars_held_view(d, warn_mult=3.0):
    """Holding time, and how far from its own level each bucket actually filled.

    A concentration at 0 or 1 bars is NOT itself a defect: gold's median entry
    bar spans more than its stop, so a third of trades genuinely resolve on the
    bar they open on. What WAS a defect is filling a long way from the level in
    that bucket while every other bucket fills on it -- that is the shape the
    entry-bar blind spot made, and it is the thing to warn on. Distance from the
    level, per bucket; never the size of the bucket.
    """
    h = (d.groupby(["bars_held", "exit_reason"]).size()
         .unstack(fill_value=0).sort_index())
    h["total"] = h.sum(axis=1)
    h["share_%"] = 100.0 * h["total"] / len(d)
    if {"sl_price", "tp_price", "exit_price", "exit_reason"} <= set(d.columns):
        w = d.copy()
        lvl = np.where(w.exit_reason == "tp", w.tp_price, w.sl_price)
        w["off_level"] = (w.exit_price - lvl).abs()
        w.loc[~w.exit_reason.isin(["sl", "tp", "be_stop"]), "off_level"] = np.nan
        h["mean_off_level"] = w.groupby("bars_held").off_level.mean()
    _show("holding time in bars, by exit reason", h.head(15),
          "mean_off_level is how far the fill landed from the level it was meant "
          "to fill at.\nAbout one spread everywhere is healthy; a bucket where it "
          "is not is an execution\nrule resolving on the wrong bar.")

    col = h.get("mean_off_level")
    if col is None:
        return h
    col = col.dropna()
    early = col[col.index <= 1]
    settled = col[col.index >= 3]
    if len(early) and len(settled) and settled.mean() > 0 \
            and early.max() > warn_mult * settled.mean():
        print("\nWARNING: exits in the first bar fill %.3f from their level, "
              "against %.3f for\n         trades held 3+ bars. A market gap does "
              "not sort itself by holding time,\n         so that is an execution "
              "rule resolving on the wrong bar -- see 'The\n         entry-bar "
              "blind spot' in CLAUDE.md."
              % (early.max(), settled.mean()))
    return h


# --- 2. regime: what conditions the trade was opened into -------------------

def regime_view(d, q=5):
    """Quintiles of the conditions recorded AT ENTRY.

    `r_price / band_at_entry` is the one worth reading first: it is the stop
    distance measured in the envelope's own half-widths, i.e. how much of the
    signal's dispersion the trade is risking. A fixed pip stop is a different
    fraction of that on every instrument and in every regime, which is exactly
    what a shared pip count cannot express.
    """
    out = {}
    d = d.copy()
    if {"r_price", "band_at_entry"} <= set(d.columns):
        d["sl_over_band"] = d.r_price / d.band_at_entry.replace(0, np.nan)
    for col in ("sl_over_band", "atr_at_entry", "band_at_entry",
                "spread_at_entry", "signal_strength"):
        if col not in d.columns or d[col].nunique() < q:
            continue
        try:
            d["_q"] = pd.qcut(d[col], q, labels=False, duplicates="drop")
        except ValueError:
            continue
        g = d.groupby("_q").agg(
            n=("net_pl", "size"), lo=(col, "min"), hi=(col, "max"),
            win_rate=("net_pl", lambda s: 100.0 * (s > 0).mean()),
            net_pl=("net_pl", "sum"), per_trade=("net_pl", "mean"),
            expectancy_r=("pnl_r", "mean") if "pnl_r" in d.columns
            else ("net_pl", "mean"))
        out[col] = g
        _show("by %s (quintiles, 0 = lowest)" % col, g)
    return out


# --- 3. excursion: what the trades actually did before resolving ------------

def excursion_view(d, steps=None):
    """What a different stop or target would have done to THESE trades.

    Not a backtest -- filtering changes which trades exist downstream, so this
    cannot tell you the P&L of a different geometry. What it does tell you is
    the shape of the excursions, which bounds the grid worth sweeping: a target
    beyond the 90th percentile of winners' MFE is a target the strategy almost
    never reaches, and there is no point spending an hour measuring it.
    """
    if not {"mae_r", "mfe_r"} <= set(d.columns):
        return None
    win = d[d.net_pl > 0]
    los = d[d.net_pl <= 0]
    q = [0.1, 0.25, 0.5, 0.75, 0.9]
    summary = pd.DataFrame({
        "winners_mae_r": win.mae_r.quantile(q).values,
        "winners_mfe_r": win.mfe_r.quantile(q).values,
        "losers_mae_r": los.mae_r.quantile(q).values,
        "losers_mfe_r": los.mfe_r.quantile(q).values,
    }, index=["p10", "p25", "p50", "p75", "p90"])
    _show("excursion in R, winners vs losers", summary,
          "winners' MAE is how much heat a win took -- the case for the stop "
          "size.\nlosers' MFE is how much open profit a loss gave back -- the "
          "case for a trailing stop or a nearer target.")

    steps = steps or [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
    rows = []
    for s in steps:
        rows.append({
            "level_R": s,
            "winners_stopped_by_a_%gR_stop" % s: int((win.mae_r < -s).sum()),
            "losers_saved_by_a_%gR_stop" % s: int((los.mae_r > -s).sum()),
            "trades_reaching_a_%gR_target" % s: int((d.mfe_r >= s).sum()),
        })
    tbl = pd.DataFrame([{
        "level_R": r["level_R"],
        "winners_a_tighter_stop_would_kill": list(r.values())[1],
        "losers_a_tighter_stop_would_spare": list(r.values())[2],
        "trades_reaching_this_as_a_target": list(r.values())[3],
    } for r in rows]).set_index("level_R")
    _show("what moving the stop or the target would have touched", tbl,
          "counts only -- this is the shape of the excursions, NOT the P&L of "
          "another configuration.")
    return summary


# --- 4. streaks -------------------------------------------------------------

def streak_view(d):
    signs = np.sign(d.net_pl.values)
    runs, cur, prev = [], 0, 0
    for s in signs:
        if s == prev and s != 0:
            cur += 1
        else:
            if prev != 0:
                runs.append((prev, cur))
            prev, cur = s, 1
    if prev != 0:
        runs.append((prev, cur))
    wins = [n for s, n in runs if s > 0]
    losses = [n for s, n in runs if s < 0]
    p_loss = 1.0 - (d.net_pl > 0).mean()
    n = max(len(d), 1)
    expected = np.log(n * max(p_loss, 1e-9)) / -np.log(max(p_loss, 1e-9)) \
        if 0 < p_loss < 1 else float("nan")
    print("\n--- streaks ---")
    print("longest win run   %d   (runs: %s)" % (max(wins or [0]), len(wins)))
    print("longest loss run  %d   (runs: %s)" % (max(losses or [0]), len(losses)))
    print("an iid sequence at this win rate would already produce about %.1f;"
          "\nonly a materially longer run is evidence of a defect." % expected)


# --- 5. what the broker took ------------------------------------------------

def cost_view(d, key="session"):
    """The gap between the movement captured and the money kept.

    Pips are gross and blind to size; net_pl is not. Their difference IS the
    cost, and no other surface in this project shows it per bucket -- which is
    what a spread filter would have to be argued from.
    """
    if "pips" not in d.columns or key not in d.columns:
        return None
    d = d.copy()
    pip = d.get("pip_size")
    if pip is None or not (pip > 0).any():
        return None
    g = d.groupby(key).agg(n=("net_pl", "size"), net_pips=("pips", "sum"),
                           net_pl=("net_pl", "sum"),
                           spread=("spread_at_entry", "mean"))
    # money the movement alone would have produced, at this run's volume
    g["pl_per_pip"] = g.net_pl / g.net_pips.replace(0, np.nan)
    _show("captured movement vs money kept, by %s" % key, g)
    return g


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("ledger", help="path to a *_ledger.csv")
    p.add_argument("--groups", default="session,day_of_week,side,exit_reason,hour_utc")
    args = p.parse_args(argv)

    d = load(args.ledger)
    print("%s -- %d closed trades, %s -> %s"
          % (os.path.basename(args.ledger), len(d),
             d.entry_time.min() if "entry_time" in d else "?",
             d.exit_time.max() if "exit_time" in d else "?"))
    print("net P&L %.2f   win rate %.2f%%"
          % (d.net_pl.sum(), 100.0 * (d.net_pl > 0).mean()))

    bars_held_view(d)
    for key in [g.strip() for g in args.groups.split(",") if g.strip()]:
        _show("by %s" % key, by_group(d, key))
    regime_view(d)
    excursion_view(d)
    streak_view(d)
    cost_view(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
