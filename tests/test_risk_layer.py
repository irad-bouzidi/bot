"""Position sizing and the caps that bound a losing run.

`Trading Bot.md` section 7 asks for risk-based sizing plus a maximum daily loss,
consecutive-loss protection and a maximum open exposure. Each test here pins one
of them, and the first pins the property that let the whole layer ship at all:
turned off, it changes nothing.
"""

import numpy as np
import pandas as pd
import pytest

from backend.backtest.engine import BacktestConfig, BacktestEngine
from backend.backtest.ledger import EXIT_RISK, EXIT_SL
from backend.backtest.risk import RiskConfig, size_for_risk
from backend.core.types import Side, Signal, SignalType, SymbolSpec
from backend.data.market_data import BarSet
from backend.strategy.base import Strategy

# tick_value == tick_size at volume 1.0, so one unit of PRICE is one unit of
# MONEY and every expected number below is arithmetic rather than a guess.
SPEC = SymbolSpec(name="TEST", digits=2, point=0.01, tick_size=0.01,
                  tick_value=0.01, contract_size=1.0,
                  volume_min=0.01, volume_step=0.01, volume_max=100.0)


def bars(rows, freq="5min"):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq=freq, tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df.index.name = "time"
    df["spread"] = 0
    df["tick_volume"] = 1
    df["real_volume"] = 0
    return BarSet(df=df, spec=SPEC, symbol="TEST", timeframe="M5", warmup_count=0)


class EnterEvery(Strategy):
    """Enters long whenever flat, with fixed distances. Used to drive streaks."""

    def __init__(self, sl=10.0, tp=10.0, every=True, entry_bar=0):
        self.sl, self.tp, self.every, self.entry_bar = sl, tp, every, entry_bar

    def warmup_bars(self):
        return 0

    def feature_names(self):
        return ["dummy"]

    def prepare(self, b):
        return pd.DataFrame({"dummy": np.zeros(len(b))}, index=b.index)

    def on_bar(self, ctx):
        if ctx.position is not None:
            return []
        if self.every or ctx.index == self.entry_bar:
            return [Signal(SignalType.ENTER_LONG, "test", ctx.bar.close,
                           sl_distance=self.sl, tp_distance=self.tp)]
        return []


def run(bs, strat, cfg=None):
    return BacktestEngine(strat, SPEC, cfg=cfg or BacktestConfig(
        initial_balance=1000.0, volume=1.0)).run(bs)


# --- the gate that let this ship ------------------------------------------

def test_risk_config_defaults_reproduce_the_fixed_volume_engine_exactly():
    """`RiskConfig()` must be a no-op. A non-zero default would silently re-size
    every stored configuration and every other test the moment this landed."""
    rows = [(100, 101, 99, 100)] * 3 + [(100, 101, 85, 90)] + [(90, 91, 89, 90)] * 2
    off = run(bars(rows), EnterEvery(entry_bar=0, every=False))
    on = run(bars(rows), EnterEvery(entry_bar=0, every=False),
             cfg=BacktestConfig(initial_balance=1000.0, volume=1.0,
                                risk=RiskConfig()))
    pd.testing.assert_frame_equal(off.ledger, on.ledger)
    assert off.metrics["total_pl"] == on.metrics["total_pl"]


def test_a_config_this_engine_cannot_honour_is_refused_not_downgraded():
    """`open_trade` is a scalar, so the engine is single-position by
    construction. Accepting max_open_positions=3 would silently backtest a
    different strategy from the one asked for."""
    with pytest.raises(ValueError) as exc:
        RiskConfig(max_open_positions=3)
    assert "one position" in str(exc.value)


# --- sizing ----------------------------------------------------------------

def test_risk_sizing_puts_the_configured_percent_at_the_stop():
    # 1% of $1,000 = $10 at risk; a 10-point stop at $1/point gives 1.0 lot.
    lots = size_for_risk(RiskConfig(risk_pct_per_trade=1.0), SPEC,
                         equity=1000.0, entry_price=100.0, sl_price=90.0,
                         side_sign=+1)
    assert lots == pytest.approx(1.0)


def test_the_size_rounds_DOWN_so_it_never_risks_more_than_asked():
    # $7 at risk over a 10-point stop is 0.7 lots exactly; make it not divide.
    lots = size_for_risk(RiskConfig(risk_pct_per_trade=0.77), SPEC,
                         equity=1000.0, entry_price=100.0, sl_price=90.0,
                         side_sign=+1)
    assert lots == pytest.approx(0.77)
    tight = size_for_risk(RiskConfig(risk_pct_per_trade=1.0), SPEC,
                          equity=1000.0, entry_price=100.0, sl_price=97.0,
                          side_sign=+1)
    assert tight <= 10.0 / 3.0 + 1e-9, "must never round UP past the risk asked"


def test_a_size_below_the_brokers_minimum_is_skipped_not_clamped_up():
    """`round_volume` clamps UP to volume_min, so clamping here would risk more
    than requested precisely when the account is smallest."""
    rc = RiskConfig(risk_pct_per_trade=0.005)     # $0.05 at risk on $1,000
    assert size_for_risk(rc, SPEC, 1000.0, 100.0, 90.0, +1) is None


def test_clamp_policy_is_opt_in_and_visibly_over_risks():
    rc = RiskConfig(risk_pct_per_trade=0.005, min_volume_policy="clamp")
    lots = size_for_risk(rc, SPEC, 1000.0, 100.0, 90.0, +1)
    assert lots == pytest.approx(SPEC.volume_min)
    # 0.01 lots over a 10-point stop risks $0.10, against the $0.10 asked -- the
    # over-risk appears when the minimum is coarse relative to the account.
    assert lots * 10.0 >= 1000.0 * rc.risk_pct_per_trade / 100.0


def test_sizing_is_skipped_when_the_stop_distance_is_unusable():
    rc = RiskConfig(risk_pct_per_trade=1.0)
    assert size_for_risk(rc, SPEC, 1000.0, 100.0, 100.0, +1) is None
    assert size_for_risk(rc, SPEC, 0.0, 100.0, 90.0, +1) is None


def test_one_R_stays_the_risk_taken_at_entry_when_sizes_vary():
    """1R is anchored per trade to the volume OPENED, so a run whose size
    changes with equity still reports comparable R."""
    rows = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 85, 88),
            (88, 89, 87, 88), (88, 89, 87, 88), (88, 89, 75, 78),
            (78, 79, 77, 78)]
    res = run(bars(rows), EnterEvery(sl=10.0, tp=100.0),
              cfg=BacktestConfig(initial_balance=1000.0, volume=1.0,
                                 risk=RiskConfig(risk_pct_per_trade=1.0)))
    closed = res.ledger[~res.ledger.is_open]
    stops = closed[closed.exit_reason == EXIT_SL]
    assert len(stops) >= 2
    assert stops.volume.nunique() > 1, "sizes must actually differ for this to bite"
    for r in stops.pnl_r:
        assert r == pytest.approx(-1.0, abs=0.02)


# --- the caps --------------------------------------------------------------

def test_the_equity_floor_produces_a_risk_halt_exit_reason():
    """`EXIT_RISK` was defined and never produced by anything. It is the label
    that says a run stopped early rather than finishing."""
    rows = [(100, 101, 99, 100), (100, 101, 99, 100)] + [(100, 101, 10, 20)] \
        + [(20, 21, 19, 20)] * 3
    res = run(bars(rows), EnterEvery(sl=500.0, tp=500.0),
              cfg=BacktestConfig(initial_balance=100.0, volume=1.0,
                                 risk=RiskConfig(min_equity=50.0)))
    assert res.metrics["halted"] is True
    assert "floor" in res.metrics["halt_reason"]
    assert EXIT_RISK in set(res.ledger.exit_reason)


def test_a_halted_run_says_so_in_its_metrics_so_it_can_be_filtered_out():
    """A halted run's total_pl is not comparable with a completed one; a sweep
    that cannot see this can be won by a configuration that blew up on day 30."""
    rows = [(100, 101, 99, 100), (100, 101, 99, 100)] + [(100, 101, 10, 20)] \
        + [(20, 21, 19, 20)] * 3
    res = run(bars(rows), EnterEvery(sl=500.0, tp=500.0),
              cfg=BacktestConfig(initial_balance=100.0, volume=1.0,
                                 risk=RiskConfig(min_equity=50.0)))
    assert res.metrics["halted"] is True
    assert res.metrics["halt_time"]
    assert res.metrics["min_equity_seen"] <= 50.0


def test_no_new_entries_are_taken_after_a_halt():
    rows = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 10, 20)] \
        + [(20, 21, 19, 20)] * 8
    res = run(bars(rows), EnterEvery(sl=500.0, tp=500.0),
              cfg=BacktestConfig(initial_balance=100.0, volume=1.0,
                                 risk=RiskConfig(min_equity=50.0)))
    assert res.metrics["entries_blocked_halted"] >= 1
    assert res.metrics["trades_opened"] == 1


def test_consecutive_losses_start_a_cooldown_measured_in_bars():
    stair = [(100, 101, 99, 100), (100, 101, 99, 100)]
    for k in range(6):
        base = 100 - 20 * k
        stair += [(base, base + 1, base - 15, base - 12),
                  (base - 12, base - 11, base - 13, base - 12)]
    plain = run(bars(stair), EnterEvery(sl=10.0, tp=100.0))
    cooled = run(bars(stair), EnterEvery(sl=10.0, tp=100.0),
                 cfg=BacktestConfig(initial_balance=1000.0, volume=1.0,
                                    risk=RiskConfig(max_consecutive_losses=2,
                                                    cooldown_bars=3)))
    assert cooled.metrics["entries_blocked_cooldown"] > 0
    assert cooled.metrics["trades_opened"] < plain.metrics["trades_opened"]


def test_the_daily_loss_cap_blocks_entries_and_resets_the_next_day():
    """Two broker days of identical losing bars. The cap must bite on each day
    separately, not latch on permanently."""
    # Every bar after the first fills at 100, stops at 90 and closes back at
    # 100, so each bar is exactly one -$10 trade and the cap's arithmetic is
    # visible: 2% of $1,000 is $20, i.e. two losses.
    bs = bars([(100, 101, 99, 100)] + [(100, 101, 88, 100)] * 29, freq="1h")
    res = run(bs, EnterEvery(sl=10.0, tp=100.0),
              cfg=BacktestConfig(initial_balance=1000.0, volume=1.0,
                                 risk=RiskConfig(max_daily_loss_pct=2.0)))
    assert res.metrics["entries_blocked_daily_loss"] > 0
    # Blocked on both days, so trades are spread across them rather than all
    # taken before a single latch.
    days = pd.to_datetime(res.ledger.entry_time, utc=True).dt.date.nunique()
    assert days == 2, "the cap must reset, not latch for the whole run"


def test_the_daily_loss_cap_does_not_close_a_running_position():
    """Closing one would realise the very loss the cap exists to bound."""
    rows = [(100, 101, 99, 100), (100, 101, 99, 100)] + [(100, 101, 60, 70)] \
        + [(70, 71, 69, 70)] * 4
    res = run(bars(rows), EnterEvery(sl=500.0, tp=500.0),
              cfg=BacktestConfig(initial_balance=1000.0, volume=1.0,
                                 risk=RiskConfig(max_daily_loss_pct=1.0)))
    assert not res.metrics["halted"]
    assert EXIT_RISK not in set(res.ledger.exit_reason)


def test_blocked_entries_are_counted_by_reason():
    """A rule you cannot count is a rule you cannot evaluate -- the mirror of
    `Trading Bot.md` rule 6."""
    res = run(bars([(100, 101, 99, 100)] * 4), EnterEvery(sl=10.0, tp=10.0),
              cfg=BacktestConfig(initial_balance=1000.0, volume=1.0,
                                 risk=RiskConfig(min_equity=1.0)))
    for key in ("daily_loss", "cooldown", "halted"):
        assert "entries_blocked_" + key in res.metrics
