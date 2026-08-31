"""Performance metrics computed from the trade ledger and the equity curve.

Honesty rules baked in, because these numbers get over-read at this sample size:

* Sharpe/Sortino are computed on a CALENDAR DAILY equity series, never on the
  trade sequence. `SR_trade * sqrt(trades_per_year)` inflates mechanically with
  trade frequency and assumes independence across trades minutes apart.
* Sortino's downside deviation divides by the FULL day count, not the number of
  negative days. Dividing by the negative count is the common implementation
  error and inflates Sortino by roughly sqrt(N/N_neg).
* Every ratio ships with `*_note` / `*_se` fields flagging when it is not
  distinguishable from zero. An annualised Sharpe from one year of data has a
  standard error near +/-1.0, so ranking configurations by Sharpe is meaningless
  here and the report says so rather than printing a bare number.
* `max_consecutive_losses` is reported next to its iid expectation, because at
  p=0.5 and n=300 an 8-loss streak is expected, not a defect.

Wins/losses are computed over CLOSED trades only; the marked-to-market survivor
counts in `trades_opened` and `final_balance` but not in the win rate.
"""

import math
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


def _streak(mask):
    # type: (np.ndarray) -> int
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def _expected_max_streak(p, n):
    # type: (float, int) -> Optional[float]
    """E[longest run] ~ log_{1/p}(n(1-p)) for iid Bernoulli."""
    if n <= 0 or not (0 < p < 1):
        return None
    try:
        return math.log(n * (1 - p)) / math.log(1.0 / p)
    except (ValueError, ZeroDivisionError):
        return None


def daily_returns(equity):
    # type: (pd.Series) -> pd.Series
    if equity is None or len(equity) == 0:
        return pd.Series(dtype=float)
    daily = equity.resample("1D").last().dropna()
    return daily.pct_change().dropna()


def compute_metrics(ledger, equity, initial_balance, periods_per_year=252):
    # type: (pd.DataFrame, pd.Series, float, int) -> Dict[str, Any]
    m = {}  # type: Dict[str, Any]
    final_balance = float(equity.iloc[-1]) if len(equity) else initial_balance

    n_all = int(len(ledger))
    closed = ledger[~ledger["is_open"]] if n_all else ledger
    n = int(len(closed))

    r = closed["net_pl"].values if n else np.array([])
    wins_mask = r > 0
    loss_mask = r < 0
    gross_profit = float(r[wins_mask].sum()) if n else 0.0
    gross_loss = float(-r[loss_mask].sum()) if n else 0.0
    n_win, n_loss = int(wins_mask.sum()), int(loss_mask.sum())

    m["initial_balance"] = float(initial_balance)
    m["final_balance"] = final_balance
    m["total_pl"] = float(final_balance - initial_balance)
    m["trades_opened"] = n_all
    m["closed_trades"] = n
    m["open_trades"] = int(n_all - n)
    m["wins"] = n_win
    m["losses"] = n_loss
    m["scratches"] = int(n - n_win - n_loss)
    m["win_rate"] = 100.0 * n_win / n if n else 0.0
    m["gross_profit"] = gross_profit
    m["gross_loss"] = gross_loss
    m["profit_factor"] = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    m["avg_win"] = gross_profit / n_win if n_win else 0.0
    m["avg_loss"] = gross_loss / n_loss if n_loss else 0.0
    m["avg_trade"] = float(r.mean()) if n else 0.0
    m["expectancy"] = m["avg_trade"]
    m["realized_rr"] = (m["avg_win"] / m["avg_loss"]) if m["avg_loss"] > 0 else float("inf")
    m["largest_win"] = float(r.max()) if n else 0.0
    m["largest_loss"] = float(r.min()) if n else 0.0
    m["roi_pct"] = 100.0 * m["total_pl"] / initial_balance if initial_balance else 0.0

    # Expectancy in R -- the scale- and symbol-free number worth comparing.
    if n and "pnl_r" in closed:
        rr = closed["pnl_r"].values
        m["expectancy_r"] = float(np.mean(rr))
        m["expectancy_r_se"] = float(np.std(rr, ddof=1) / math.sqrt(n)) if n > 1 else None
        m["total_r"] = float(np.sum(rr))
    else:
        m["expectancy_r"] = m["expectancy_r_se"] = m["total_r"] = None

    m["max_consecutive_wins"] = _streak(wins_mask)
    m["max_consecutive_losses"] = _streak(loss_mask)
    exp_streak = _expected_max_streak(1 - (n_win / n) if n else 0.5, n)
    m["max_consecutive_losses_expected_iid"] = (
        round(exp_streak, 1) if exp_streak else None)
    m["max_consecutive_losses_note"] = (
        "An iid sequence with this win rate would already produce a streak of "
        "about %.1f; only a materially longer run is evidence of a defect."
        % exp_streak if exp_streak else None)

    # Drawdown from the EQUITY curve (includes open-position risk).
    if len(equity):
        peak = equity.cummax()
        dd_abs = peak - equity
        dd_pct = (dd_abs / peak.replace(0, np.nan)) * 100.0
        m["max_drawdown"] = float(dd_pct.max()) if len(dd_pct.dropna()) else 0.0
        m["max_drawdown_abs"] = float(dd_abs.max())
    else:
        m["max_drawdown"] = m["max_drawdown_abs"] = 0.0

    # Risk-adjusted ratios, with their own health warnings.
    dr = daily_returns(equity)
    n_days = int(len(dr))
    m["trading_days"] = n_days
    if n_days > 2 and dr.std(ddof=1) > 0:
        sr = float(dr.mean() / dr.std(ddof=1) * math.sqrt(periods_per_year))
        downside = float(np.sqrt(np.mean(np.minimum(dr.values, 0.0) ** 2)))
        m["sharpe"] = sr
        m["sortino"] = float(dr.mean() / downside * math.sqrt(periods_per_year)) \
            if downside > 0 else None
        se = math.sqrt((1 + sr * sr / 2.0) / n_days) * math.sqrt(periods_per_year)
        m["sharpe_se"] = se
        m["sharpe_note"] = (
            "NOT distinguishable from zero (|SR| < 2*SE); do not rank configurations "
            "by this." if abs(sr) < 2 * se else
            "Clears 2*SE, but still a single-sample estimate.")
    else:
        m["sharpe"] = m["sortino"] = m["sharpe_se"] = None
        m["sharpe_note"] = "Too few daily observations to estimate."

    if n:
        m["avg_bars_held"] = float(closed["bars_held"].mean())
        m["avg_duration_hours"] = float(closed["duration_s"].mean() / 3600.0)
        m["total_costs"] = float(closed["commission"].sum() - closed["swap"].sum())
        m["exit_reason_counts"] = closed["exit_reason"].value_counts().to_dict()
        longs, shorts = closed[closed["side"] == "long"], closed[closed["side"] == "short"]
        m["long_trades"], m["short_trades"] = int(len(longs)), int(len(shorts))
        m["long_win_rate"] = 100.0 * float((longs["net_pl"] > 0).mean()) if len(longs) else None
        m["short_win_rate"] = 100.0 * float((shorts["net_pl"] > 0).mean()) if len(shorts) else None
        m["long_pl"] = float(longs["net_pl"].sum())
        m["short_pl"] = float(shorts["net_pl"].sum())
        m["avg_mae_r"] = float(closed["mae_r"].mean())
        m["avg_mfe_r"] = float(closed["mfe_r"].mean())
        # Scale-out take-up. A rule that fires on 2% of trades cannot explain a
        # change in the headline numbers, and knowing that before arguing about
        # the P&L saves the argument.
        if "partial_volume" in closed:
            fired = closed[closed["partial_volume"] > 0]
            m["partials_fired"] = int(len(fired))
            m["partials_fired_pct"] = 100.0 * len(fired) / n
            m["partial_pl"] = float(fired["partial_pl"].sum()) if len(fired) else 0.0
        else:
            m["partials_fired"] = 0
            m["partials_fired_pct"] = 0.0
            m["partial_pl"] = 0.0
    else:
        for k in ("avg_bars_held", "avg_duration_hours", "total_costs",
                  "long_win_rate", "short_win_rate", "avg_mae_r", "avg_mfe_r"):
            m[k] = None
        m["partials_fired"] = 0
        m["partials_fired_pct"] = 0.0
        m["partial_pl"] = 0.0
        m["exit_reason_counts"] = {}
        m["long_trades"] = m["short_trades"] = 0
        m["long_pl"] = m["short_pl"] = 0.0

    span_days = (equity.index[-1] - equity.index[0]).total_seconds() / 86400.0 \
        if len(equity) > 1 else 0.0
    m["span_days"] = round(span_days, 1)
    m["trades_per_day"] = round(n / span_days, 3) if span_days > 0 else None

    m["insufficient_sample"] = n < 30
    if n < 30:
        m["sample_note"] = (
            "Only %d closed trades. Profit factor, drawdown and the ratios are "
            "not interpretable at this size." % n)
    return m


def by_group(ledger, key):
    # type: (pd.DataFrame, str) -> pd.DataFrame
    """Breakdown by session / day_of_week / side / exit_reason (spec sections 9-10).

    `n` is always returned so a seductive win rate on 4 trades is visible as such.
    """
    if ledger is None or not len(ledger) or key not in ledger.columns:
        return pd.DataFrame()
    closed = ledger[~ledger["is_open"]]
    if not len(closed):
        return pd.DataFrame()
    g = closed.groupby(key)
    out = pd.DataFrame({
        "n": g.size(),
        "win_rate": g["net_pl"].apply(lambda s: 100.0 * (s > 0).mean()),
        "net_pl": g["net_pl"].sum(),
        "avg_pl": g["net_pl"].mean(),
        "expectancy_r": g["pnl_r"].mean() if "pnl_r" in closed else np.nan,
        "profit_factor": g["net_pl"].apply(
            lambda s: s[s > 0].sum() / abs(s[s < 0].sum()) if (s < 0).any() else np.inf),
    })
    return out.sort_values("n", ascending=False)
