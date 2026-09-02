"""Scale-out and break-even (engine rule 9).

Same style as test_backtest_engine.py: tiny hand-built bars so every expected
fill is arithmetic. SPEC has tick_value == tick_size and contract_size 1, so at
volume=1.0 one unit of PRICE is one unit of MONEY -- a 0.6 partial at +5.00
banks exactly 3.00.

The cases that matter are the ORDERING ones. A scale-out rule is the easiest
place in a backtest to manufacture free money: let every loser bank a risk-free
profit on its way down and the equity curve improves without a single real
trade changing.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from backend.backtest.costs import CostConfig, CostModel
from backend.backtest.engine import BacktestConfig, BacktestEngine
from backend.backtest.ledger import EXIT_BE, EXIT_END_OF_DATA, EXIT_SL, EXIT_TP
from backend.core.types import Signal, SignalType, SymbolSpec
from backend.strategy.base import Strategy
from backend.strategy.nw_envelope import NWConfig, NWEnvelopeStrategy

from tests.test_backtest_engine import SPEC, bars

# volume_step 0.01 with volume 1.0 means 0.6 is representable exactly.
LOTS = 1.0


class ScaleOutStrategy(Strategy):
    """Enters long on bar 0 with SL 10, TP 10, scale-out at +5 for 60%."""

    def __init__(self, sl=10.0, tp=10.0, be=5.0, frac=0.6,
                 side=SignalType.ENTER_LONG):
        self.sl, self.tp, self.be, self.frac, self.side = sl, tp, be, frac, side

    def warmup_bars(self):
        return 0

    def feature_names(self):
        return ["dummy"]

    def prepare(self, b):
        return pd.DataFrame({"dummy": np.zeros(len(b))}, index=b.index)

    def on_bar(self, ctx):
        if ctx.index == 0 and ctx.position is None:
            return [Signal(self.side, "test", ctx.bar.close,
                           sl_distance=self.sl, tp_distance=self.tp,
                           be_trigger_distance=self.be, partial_fraction=self.frac)]
        return []


def run(bs, strat, costs=None, cfg=None):
    eng = BacktestEngine(strat, SPEC, costs=costs,
                         cfg=cfg or BacktestConfig(initial_balance=1000.0,
                                                   volume=LOTS))
    return eng.run(bs)


def only(res):
    assert len(res.ledger) == 1, res.ledger[["exit_reason", "net_pl"]]
    return res.ledger.iloc[0]


# --- the trigger fires ------------------------------------------------------

def test_partial_fires_at_the_trigger_and_banks_profit():
    # Entry fills at bar 1's open = 100. Trigger = 105, TP = 110, SL = 90.
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),     # entry fills here
               (100, 106, 100, 101),     # high 106 crosses the 105 trigger
               (101, 101, 101, 101)])
    t = only(run(bs, ScaleOutStrategy()))
    assert t["partial_volume"] == pytest.approx(0.6)
    assert t["partial_price"] == pytest.approx(105.0)     # the level, not the high
    assert t["partial_pl"] == pytest.approx(0.6 * 5.0)    # 3.00
    assert t["remaining_volume"] == pytest.approx(0.4)
    assert t["be_moved"]


def test_stop_moves_to_entry_and_the_remainder_scratches_there():
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),     # entry at 100
               (100, 106, 100, 101),     # trigger at 105 -> partial, SL -> 100
               (101, 101, 95, 96),       # low 95 takes out the break-even stop
               (96, 96, 96, 96)])
    t = only(run(bs, ScaleOutStrategy()))
    assert t["exit_reason"] == EXIT_BE          # not "sl" -- the census must show it
    assert t["exit_price"] == pytest.approx(100.0)
    # Remainder exits at entry, so the whole trade keeps only the banked partial.
    assert t["net_pl"] == pytest.approx(3.0)


def test_without_the_rule_the_same_bars_are_a_full_stop_out():
    """The comparison that justifies the rule at all: same price path, no scale-out."""
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),
               (100, 106, 100, 101),
               (101, 101, 89, 90),        # low 89 -> through the 90 stop
               (90, 90, 90, 90)])
    with_rule = only(run(bs, ScaleOutStrategy()))
    without = only(run(bs, ScaleOutStrategy(frac=0.0, be=None)))
    assert without["exit_reason"] == EXIT_SL
    assert without["net_pl"] == pytest.approx(-10.0)      # full 1R
    # With the rule: 0.6 banked at +5, 0.4 stopped at entry.
    assert with_rule["exit_reason"] == EXIT_BE
    assert with_rule["net_pl"] == pytest.approx(3.0)


# --- the ordering rules, which is where free money gets invented ------------

def test_bar_touching_both_trigger_and_stop_is_a_full_loss_with_no_partial():
    """Unresolvable from OHLC, so it must book the pessimistic reading."""
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),     # entry at 100
               (100, 106, 89, 95),       # high 106 AND low 89: order unknowable
               (95, 95, 95, 95)])
    res = run(bs, ScaleOutStrategy())
    t = only(res)
    assert t["partial_volume"] == 0.0
    assert not t["be_moved"]
    assert t["exit_reason"] == EXIT_SL
    assert t["net_pl"] == pytest.approx(-10.0)
    assert res.metrics["ambiguous_bars"] >= 1      # and it is REPORTED, not hidden


def test_reaching_tp_books_the_partial_first_because_that_order_is_known():
    # Any path from 100 to 110 passes 105, so the partial is not a guess.
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),
               (100, 111, 100, 110),     # sweeps trigger and TP in one bar
               (110, 110, 110, 110)])
    t = only(run(bs, ScaleOutStrategy()))
    assert t["partial_volume"] == pytest.approx(0.6)
    assert t["exit_reason"] == EXIT_TP
    # 0.6 out at +5 = 3.00, 0.4 out at +10 = 4.00.
    assert t["net_pl"] == pytest.approx(7.0)


def test_break_even_stop_is_not_live_on_the_bar_that_created_it():
    """The stop did not exist when this bar's low printed, so it cannot fill.

    The bar dips below entry AND reaches the target. The dip is unorderable
    against the trigger touch, so it must not produce an exit -- but it must be
    counted as ambiguous, the same way rule 4's ties are.
    """
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),
               (100, 111, 99, 105),      # trigger, TP and a dip back through entry
               (105, 105, 105, 105)])
    res = run(bs, ScaleOutStrategy())
    t = only(res)
    assert t["partial_volume"] == pytest.approx(0.6)
    assert t["exit_reason"] == EXIT_TP          # the one determinable ordering
    assert t["net_pl"] == pytest.approx(7.0)
    assert res.metrics["ambiguous_bars"] >= 1   # exposure measured, not hidden


def test_gap_past_the_trigger_fills_the_partial_at_the_open():
    # Mirror of rule 5: the first available price is the open, here in our favour.
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),     # entry at 100
               (107, 107, 106, 106),     # opens at 107, already past the 105 trigger
               (106, 106, 106, 106)])
    t = only(run(bs, ScaleOutStrategy()))
    assert t["partial_price"] == pytest.approx(107.0)
    assert t["partial_pl"] == pytest.approx(0.6 * 7.0)


def test_break_even_stop_can_be_gapped_through_on_a_later_bar():
    """Once the stop exists at the open, rule 5 applies to it like any other."""
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),
               (100, 106, 100, 101),     # partial fires, SL -> 100
               (94, 94, 93, 93),         # gaps below the break-even stop
               (93, 93, 93, 93)])
    t = only(run(bs, ScaleOutStrategy()))
    assert t["exit_reason"] == EXIT_BE
    assert t["exit_price"] == pytest.approx(94.0)   # the gap price, not 100
    assert t["net_pl"] == pytest.approx(3.0 + 0.4 * -6.0)


def test_stop_moved_midbar_is_not_gapped_through_by_that_bars_own_open():
    """The break-even stop did not exist when this bar opened, so no gap fill."""
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),     # entry at 100
               (98, 106, 98, 105),       # opens BELOW entry, then rallies past 105
               (105, 105, 105, 105)])
    t = only(run(bs, ScaleOutStrategy()))
    assert t["partial_volume"] == pytest.approx(0.6)
    # Must not book a "gap" exit at the 98 open against a stop created later.
    assert t["exit_reason"] == EXIT_END_OF_DATA


# --- shorts mirror longs ----------------------------------------------------

def test_short_scale_out_mirrors_the_long_case():
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),     # short entry at 100, SL 110, TP 90, be 95
               (100, 100, 94, 99),       # low 94 crosses the 95 trigger
               (99, 99, 99, 99),
               (99, 105, 99, 104)])      # back up through entry -> break-even stop
    t = only(run(bs, ScaleOutStrategy(side=SignalType.ENTER_SHORT)))
    assert t["partial_price"] == pytest.approx(95.0)
    assert t["partial_pl"] == pytest.approx(0.6 * 5.0)
    assert t["exit_reason"] == EXIT_BE
    assert t["net_pl"] == pytest.approx(3.0)


# --- accounting -------------------------------------------------------------

def test_partial_is_banked_once_not_twice():
    """The balance delta and the ledger must agree -- the double-count trap."""
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),
               (100, 106, 100, 101),
               (101, 111, 101, 110),
               (110, 110, 110, 110)])
    res = run(bs, ScaleOutStrategy())
    t = only(res)
    assert res.metrics["final_balance"] - 1000.0 == pytest.approx(t["net_pl"])
    assert t["gross_pl"] == pytest.approx(t["partial_pl"] + 0.4 * 10.0)


def test_one_R_stays_the_risk_taken_at_entry():
    """pnl_r must not be re-based on the surviving remainder."""
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),
               (100, 106, 100, 101),
               (101, 101, 95, 96),
               (96, 96, 96, 96)])
    t = only(run(bs, ScaleOutStrategy()))
    # Risked 10.00 on 1.0 lot = 10.00 of money; banked 3.00 -> +0.3R.
    assert t["r_price"] == pytest.approx(10.0)
    assert t["pnl_r"] == pytest.approx(0.3)


def test_commission_is_charged_on_the_volume_opened_not_the_remainder():
    costs = CostModel(CostConfig(spread_source="none",
                                 commission_per_lot_round_turn=10.0), SPEC)
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),
               (100, 106, 100, 101),
               (101, 101, 95, 96),
               (96, 96, 96, 96)])
    t = only(run(bs, ScaleOutStrategy(), costs=costs))
    assert t["commission"] == pytest.approx(10.0)      # 1.0 lot opened, not 0.4


def test_the_trigger_is_measured_from_the_entry_FILL_not_the_signal_price():
    """Costs push the entry up, so the trigger moves with it.

    This is the property that makes "+5.00 in profit" mean what it says. Anchoring
    the trigger to the signal price instead would arm it before the position was
    actually 5.00 up, by exactly one spread.
    """
    costs = CostModel(CostConfig(spread_source="fixed", fixed_spread_points=100), SPEC)
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),     # entry: open 100 + 1.00 spread = 101.00
               (100, 106, 100, 101),     # so the trigger is at 106, not 105
               (101, 101, 101, 101)])
    t = only(run(bs, ScaleOutStrategy(), costs=costs))
    assert t["entry_price"] == pytest.approx(101.0)
    assert t["be_trigger_price"] == pytest.approx(106.0)
    assert t["partial_price"] == pytest.approx(106.0)   # long exits at bid: no spread
    assert t["partial_pl"] == pytest.approx(0.6 * 5.0)  # a true 5.00 of profit


# --- degenerate configs fail safe ------------------------------------------

def test_a_trigger_at_or_past_the_target_is_treated_as_disabled():
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),
               (100, 111, 100, 110),
               (110, 110, 110, 110)])
    t = only(run(bs, ScaleOutStrategy(be=10.0)))     # trigger == TP
    assert t["be_trigger_price"] == 0.0
    assert t["partial_volume"] == 0.0
    assert t["net_pl"] == pytest.approx(10.0)        # plain TP on the full lot


def test_position_too_small_to_split_still_gets_the_break_even_move():
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),
               (100, 106, 100, 101),
               (101, 101, 95, 96),
               (96, 96, 96, 96)])
    # volume_min is 0.01, so 0.01 lots cannot leave a legal remainder.
    res = run(bs, ScaleOutStrategy(),
              cfg=BacktestConfig(initial_balance=1000.0, volume=0.01))
    t = only(res)
    assert t["partial_volume"] == 0.0
    assert t["be_moved"]
    assert t["exit_reason"] == EXIT_BE
    assert t["net_pl"] == pytest.approx(0.0)


def test_zero_fraction_reproduces_the_old_behaviour_exactly():
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),
               (100, 106, 100, 101),
               (101, 101, 89, 90),
               (90, 90, 90, 90)])
    off = only(run(bs, ScaleOutStrategy(frac=0.0, be=None)))
    assert off["partial_volume"] == 0.0
    assert off["be_trigger_price"] == 0.0
    assert off["exit_reason"] == EXIT_SL
    assert off["net_pl"] == pytest.approx(-10.0)


# --- the strategy emits the rule -------------------------------------------

def test_nwconfig_tp_fraction_scales_the_trigger_with_the_target():
    """The reason tp_fraction is the default: the trigger tracks the target.

    5.00 at the 10-point gold target, 25.00 at a 50-point one -- a hardcoded
    distance would silently become a different share of a different target.
    """
    gold = NWEnvelopeStrategy(NWConfig(sl_price=7.0, tp_price=10.0))
    wide = NWEnvelopeStrategy(NWConfig(sl_price=70.0, tp_price=50.0))
    assert gold._be_trigger_distance(7.0, 10.0) == pytest.approx(5.0)
    assert wide._be_trigger_distance(70.0, 50.0) == pytest.approx(25.0)


def test_be_trigger_modes():
    fixed = NWEnvelopeStrategy(NWConfig(be_trigger_mode="fixed", be_trigger_price=4.0))
    r_mode = NWEnvelopeStrategy(NWConfig(be_trigger_mode="r", be_trigger_r=0.7))
    off = NWEnvelopeStrategy(NWConfig(be_trigger_mode="none"))
    assert fixed._be_trigger_distance(7.0, 10.0) == pytest.approx(4.0)
    assert r_mode._be_trigger_distance(10.0, 30.0) == pytest.approx(7.0)
    assert off._be_trigger_distance(7.0, 10.0) is None
    # partial_fraction 0 disables it whatever the mode says.
    assert NWEnvelopeStrategy(
        NWConfig(partial_fraction=0.0))._be_trigger_distance(7.0, 10.0) is None


def test_invalid_scale_out_config_is_rejected_at_construction():
    with pytest.raises(ValueError):
        NWEnvelopeStrategy(NWConfig(be_trigger_mode="halfway"))
    with pytest.raises(ValueError):
        NWEnvelopeStrategy(NWConfig(partial_fraction=1.0))


def test_metrics_report_scale_out_take_up():
    bs = bars([(100, 100, 100, 100),
               (100, 100, 100, 100),
               (100, 106, 100, 101),
               (101, 101, 95, 96),
               (96, 96, 96, 96)])
    m = run(bs, ScaleOutStrategy()).metrics
    assert m["partials_fired"] == 1
    assert m["partials_fired_pct"] == pytest.approx(100.0)
    assert m["partial_pl"] == pytest.approx(3.0)
