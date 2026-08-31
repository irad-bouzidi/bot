"""Engine execution rules.

Each test pins one of the biases the original backtest had. They use tiny
hand-built bar sequences so the expected fill is arithmetic, not a guess.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from backend.backtest.costs import CostConfig, CostModel
from backend.backtest.engine import BacktestConfig, BacktestEngine
from backend.backtest.ledger import EXIT_END_OF_DATA, EXIT_SL, EXIT_TP
from backend.core.types import Side, Signal, SignalType, SymbolSpec
from backend.data.market_data import BarSet
from backend.strategy.base import Strategy

# tick_value == tick_size so that, at volume=1.0, one unit of PRICE equals one
# unit of MONEY. That keeps the arithmetic in these tests readable.
SPEC = SymbolSpec(name="TEST", digits=2, point=0.01, tick_size=0.01,
                  tick_value=0.01, contract_size=1.0)


def bars(rows, spread=0):
    """rows: list of (open, high, low, close)."""
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="5min", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df.index.name = "time"
    df["spread"] = spread
    df["tick_volume"] = 1
    df["real_volume"] = 0
    return BarSet(df=df, spec=SPEC, symbol="TEST", timeframe="M5", warmup_count=0)


class EnterOnceStrategy(Strategy):
    """Enters long on bar 0 with fixed SL/TP distances, then never acts again."""

    def __init__(self, sl=10.0, tp=10.0, side=SignalType.ENTER_LONG, entry_bar=0):
        self.sl, self.tp, self.side, self.entry_bar = sl, tp, side, entry_bar

    def warmup_bars(self):
        return 0

    def feature_names(self):
        return ["dummy"]

    def prepare(self, b):
        return pd.DataFrame({"dummy": np.zeros(len(b))}, index=b.index)

    def on_bar(self, ctx):
        if ctx.index == self.entry_bar and ctx.position is None:
            return [Signal(self.side, "test", ctx.bar.close,
                           sl_distance=self.sl, tp_distance=self.tp)]
        return []


def run(bs, strat, costs=None, cfg=None):
    eng = BacktestEngine(strat, SPEC, costs=costs,
                         cfg=cfg or BacktestConfig(initial_balance=1000.0, volume=1.0))
    return eng.run(bs)


# --- rule 2: next-bar-open fills -------------------------------------------

def test_entry_fills_at_next_bar_open_not_signal_bar_close():
    bs = bars([(100, 101, 99, 100),      # signal here
               (105, 106, 104, 105),     # fill at THIS open = 105
               (105, 106, 104, 105)])
    res = run(bs, EnterOnceStrategy(sl=50, tp=50))
    assert res.ledger.iloc[0]["entry_price"] == pytest.approx(105.0)
    assert res.ledger.iloc[0]["entry_index"] == 1


def test_signal_on_final_bar_cannot_fill():
    bs = bars([(100, 101, 99, 100), (100, 101, 99, 100)])
    res = run(bs, EnterOnceStrategy(entry_bar=1))
    assert len(res.ledger) == 0


# --- rule 3: intrabar stops (the dominant old bias) ------------------------

def test_intrabar_stop_is_detected_even_when_close_recovers():
    """The old engine compared only `close`, so this stop-out was invisible and
    the trade was allowed to continue to a later win."""
    bs = bars([(100, 101, 99, 100),
               (100, 101, 99, 100),      # entry at 100, SL = 90
               (100, 101, 85, 100)])     # wick to 85, closes back at 100
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0))
    row = res.ledger.iloc[0]
    assert row["exit_reason"] == EXIT_SL
    assert row["exit_price"] == pytest.approx(90.0)


def test_take_profit_detected_intrabar():
    bs = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 115, 99, 100)])
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0))
    row = res.ledger.iloc[0]
    assert row["exit_reason"] == EXIT_TP
    assert row["exit_price"] == pytest.approx(110.0)


# --- rule 4: tie-break ------------------------------------------------------

def test_both_levels_touched_books_the_stop_by_default():
    bs = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 115, 85, 100)])
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0))
    assert res.ledger.iloc[0]["exit_reason"] == EXIT_SL
    assert res.metrics["ambiguous_bars"] >= 1, "unresolvable bar should be counted"


def test_tie_break_tp_first_is_opt_in():
    bs = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 115, 85, 100)])
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0),
              cfg=BacktestConfig(initial_balance=1000.0, volume=1.0,
                                 tie_break="tp_first"))
    assert res.ledger.iloc[0]["exit_reason"] == EXIT_TP


# --- rule 5: gaps -----------------------------------------------------------

def test_gap_through_the_stop_fills_at_the_gap_price_not_the_level():
    """The old code booked a clean 10-point loss on a bar that gapped 30 points
    through the stop, so losses could never be worse than ideal."""
    bs = bars([(100, 101, 99, 100),
               (100, 101, 99, 100),      # entry 100, SL 90
               (70, 72, 68, 71)])        # opens 30 BELOW the stop
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0))
    row = res.ledger.iloc[0]
    assert row["exit_reason"] == EXIT_SL
    assert row["exit_price"] == pytest.approx(70.0), "must fill at the gap, not 90"
    assert row["net_pl"] == pytest.approx(-30.0)


# --- rule 7: the survivor ---------------------------------------------------

def test_open_position_is_marked_to_market_and_excluded_from_win_rate():
    bs = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 106, 99, 105)])
    res = run(bs, EnterOnceStrategy(sl=50.0, tp=50.0))
    row = res.ledger.iloc[0]
    assert bool(row["is_open"]) is True
    assert row["exit_reason"] == EXIT_END_OF_DATA
    assert res.metrics["trades_opened"] == 1
    assert res.metrics["closed_trades"] == 0
    assert res.metrics["wins"] == 0 and res.metrics["losses"] == 0
    assert res.metrics["final_balance"] == pytest.approx(1005.0)


# --- costs ------------------------------------------------------------------

def test_round_trip_costs_exactly_one_spread_not_two():
    """MT5 bars are bid: a long pays the spread entering, a short exiting.
    Charging it twice is enough to kill an otherwise viable configuration."""
    flat = [(100, 100, 100, 100)] * 6
    cm = CostModel(CostConfig(spread_source="fixed", fixed_spread_points=50), SPEC)

    free = run(bars(flat), EnterOnceStrategy(sl=50, tp=50))
    costed = run(bars(flat), EnterOnceStrategy(sl=50, tp=50), costs=cm)

    assert free.ledger.iloc[0]["net_pl"] == pytest.approx(0.0)
    spread_price = 50 * SPEC.point                      # 0.50
    assert costed.ledger.iloc[0]["net_pl"] == pytest.approx(-spread_price, abs=1e-9)


def test_short_pays_the_spread_on_exit():
    flat = [(100, 100, 100, 100)] * 6
    cm = CostModel(CostConfig(spread_source="fixed", fixed_spread_points=50), SPEC)
    res = run(bars(flat),
              EnterOnceStrategy(sl=50, tp=50, side=SignalType.ENTER_SHORT), costs=cm)
    assert res.ledger.iloc[0]["net_pl"] == pytest.approx(-0.50, abs=1e-9)


def test_commission_is_charged_per_round_turn():
    flat = [(100, 100, 100, 100)] * 6
    cm = CostModel(CostConfig(spread_source="none",
                              commission_per_lot_round_turn=7.0), SPEC)
    res = run(bars(flat), EnterOnceStrategy(sl=50, tp=50), costs=cm)
    assert res.ledger.iloc[0]["commission"] == pytest.approx(7.0)
    assert res.ledger.iloc[0]["net_pl"] == pytest.approx(-7.0)


def test_stop_slippage_is_worse_than_entry_slippage():
    bs = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 85, 100)])
    cm = CostModel(CostConfig(spread_source="none", slippage_points_stop=100), SPEC)
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0), costs=cm)
    # SL at 90, plus 100 points (=1.00) of adverse stop slippage.
    assert res.ledger.iloc[0]["exit_price"] == pytest.approx(89.0)


# --- excursions & drawdown --------------------------------------------------

def test_mae_and_mfe_are_tracked_in_r_units():
    bs = bars([(100, 101, 99, 100),
               (100, 101, 99, 100),      # entry 100, R = 10
               (100, 120, 95, 100),      # +20 favourable, -5 adverse
               (100, 101, 99, 100)])
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=100.0))
    row = res.ledger.iloc[0]
    assert row["mfe_r"] == pytest.approx(2.0)
    assert row["mae_r"] == pytest.approx(-0.5)


def test_drawdown_uses_equity_not_realized_balance():
    """A deep unrealised excursion must show up in drawdown even though the
    trade eventually closed flat. The old balance-only calculation missed it."""
    bs = bars([(100, 101, 99, 100), (100, 101, 99, 100),
               (100, 100, 60, 61), (100, 101, 99, 100)])
    res = run(bs, EnterOnceStrategy(sl=100.0, tp=100.0))
    assert res.metrics["max_drawdown"] > 3.0
