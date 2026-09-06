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


# --- rule 3, the entry bar: SL/TP are live from the FILL --------------------
# These pin the fix for the engine's largest bias. The stop check used to start
# on the bar AFTER the fill, so the first price it ever saw was that bar's open
# -- which rule 5 then booked as a gap. On gold M5 that hit 717 of 1044
# stop-outs and filled them an average of 3.0 past a 7.00 stop, while handing
# take-profits an average 2.1 BETTER than their limit, which no broker does.
# Live the two levels ride inside the entry order, so the broker holds them from
# the fill; these tests are what keeps the backtest modelling that bot.

def test_stop_reached_on_the_entry_bar_fills_AT_the_stop():
    bs = bars([(100, 101, 99, 100),
               (100, 101, 88, 95),       # entry 100 at THIS open, low 88 < SL 90
               (95, 96, 94, 95)])
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0))
    row = res.ledger.iloc[0]
    assert row["exit_reason"] == EXIT_SL
    assert row["exit_index"] == 1, "must resolve on the entry bar, not the next one"
    assert row["exit_price"] == pytest.approx(90.0), "the level, never the next open"
    assert row["net_pl"] == pytest.approx(-10.0)


def test_target_reached_on_the_entry_bar_fills_AT_the_target():
    """A take-profit is a LIMIT: it fills at the limit and never better."""
    bs = bars([(100, 101, 99, 100),
               (100, 130, 99, 128),      # entry 100, high 130 sails past TP 110
               (128, 129, 127, 128)])
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0))
    row = res.ledger.iloc[0]
    assert row["exit_reason"] == EXIT_TP
    assert row["exit_index"] == 1
    assert row["exit_price"] == pytest.approx(110.0), "not 130, and not the next open"
    assert row["net_pl"] == pytest.approx(10.0)


def test_entry_bar_touching_both_levels_obeys_the_tie_break():
    bs = bars([(100, 101, 99, 100),
               (100, 115, 85, 100),      # entry 100; both 90 and 110 inside
               (100, 101, 99, 100)])
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0))
    assert res.ledger.iloc[0]["exit_reason"] == EXIT_SL
    assert res.metrics["ambiguous_bars"] >= 1, "entry bar ambiguity must be counted"

    tp_first = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0),
                   cfg=BacktestConfig(initial_balance=1000.0, volume=1.0,
                                      tie_break="tp_first"))
    assert tp_first.ledger.iloc[0]["exit_reason"] == EXIT_TP


def test_an_entry_bar_that_reaches_nothing_still_resolves_on_later_bars():
    """The guard moved from `>` to `>=`; it must not have become `==`."""
    bs = bars([(100, 101, 99, 100),
               (100, 101, 99, 100),      # entry 100, neither level touched
               (100, 101, 85, 100)])     # stop swept here
    row = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0)).ledger.iloc[0]
    assert row["exit_reason"] == EXIT_SL
    assert row["exit_index"] == 2
    assert row["exit_price"] == pytest.approx(90.0)


def test_a_trade_closed_on_its_entry_bar_still_records_its_excursion():
    """Excursion is tracked before the stop block, so a same-bar exit is not
    written with a flat 0.0 MAE/MFE and quietly flattened out of the
    distribution the stop distance gets fitted against."""
    bs = bars([(100, 101, 99, 100),
               (100, 120, 88, 95),       # entry 100, R = 10: +20 then -12
               (95, 96, 94, 95)])
    row = run(bs, EnterOnceStrategy(sl=10.0, tp=100.0)).ledger.iloc[0]
    assert row["exit_index"] == 1
    assert row["mae_r"] == pytest.approx(-1.2)
    assert row["mfe_r"] == pytest.approx(2.0)


def test_legacy_mode_keeps_the_old_entry_bar_skip():
    """--compare-legacy exists to measure the ORIGINAL engine's overstatement.
    Fixing the entry bar inside it would measure something else."""
    bs = bars([(100, 101, 99, 100),
               (100, 101, 88, 85),       # close 85 is past the 90 stop
               (85, 86, 84, 85)])
    cfg = BacktestConfig(initial_balance=1000.0, volume=1.0, legacy_mode=True)
    row = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0), cfg=cfg).ledger.iloc[0]
    assert row["exit_index"] == 2, "legacy must still skip the entry bar"


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


# ---------------------------------------------------------------------------
# the centre-line exit's own ledger label
# ---------------------------------------------------------------------------
# Moved here when the news blackout was removed and tests/test_news_filter.py
# went with it. Neither of these is about news: they landed in that file only
# because SIGNAL_EXIT_REASONS gained its first two entries at the same time.

def test_a_centre_line_exit_reaches_the_ledger_as_its_own_reason():
    """What makes the rule's cost measurable at all.

    `cross_center` used to fold into the generic "signal" bucket. That kept the
    one number the decision turns on -- how often the centre line intercepts a
    scaled-out runner instead of letting it reach the target -- obtainable only
    by knowing that "signal" happened to have exactly one producer. The census
    in CLAUDE.md was transposed for precisely that reason and stood uncorrected
    because nothing could check it.

    Reading an OLD report: a pre-change ledger's "signal" rows are this
    "cross_center".
    """
    from backend.strategy.nw_envelope import NWConfig, NWEnvelopeStrategy

    class Centre(NWEnvelopeStrategy):
        def warmup_bars(self):
            return 0

        def feature_names(self):
            return ["dummy"]

        def prepare(self, b):
            return pd.DataFrame({"dummy": np.zeros(len(b))}, index=b.index)

        def on_bar(self, ctx):
            if ctx.position is None and ctx.index == 0:
                return [Signal(SignalType.ENTER_LONG, "test", ctx.bar.close,
                               sl_distance=50.0, tp_distance=50.0)]
            if ctx.position is not None and ctx.index == 2:
                return [Signal(SignalType.EXIT, "cross_center", ctx.bar.close)]
            return []

    bs = bars([(100, 101, 99, 100), (100, 101, 99, 100),
               (100, 101, 99, 100), (103, 104, 102, 103)])
    eng = BacktestEngine(Centre(NWConfig()), SPEC, costs=None,
                         cfg=BacktestConfig(initial_balance=1000.0, volume=1.0))
    res = eng.run(bs)
    assert list(res.ledger["exit_reason"]) == ["cross_center"]


# --- pips: the price distance, reported beside the money --------------------

def _pips_cfg(**over):
    """A run that reports pips. pip_size=1.0 makes one unit of PRICE one pip, so
    these tests read in the same units the rest of the file does."""
    base = dict(initial_balance=1000.0, volume=1.0, pip_size=1.0)
    base.update(over)
    return BacktestConfig(**base)


def test_a_stop_out_is_reported_as_the_stop_distance_in_pips():
    bs = bars([(100, 101, 99, 100),
               (100, 101, 99, 100),      # entry at 100, SL = 90
               (100, 101, 85, 100)])
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0), cfg=_pips_cfg())
    assert res.ledger.iloc[0]["pips"] == pytest.approx(-10.0)
    assert res.metrics["pips_lost"] == pytest.approx(-10.0)
    assert res.metrics["net_pips"] == pytest.approx(-10.0)
    assert res.metrics["pip_losses"] == 1


def test_a_short_that_wins_reports_POSITIVE_pips():
    """The sign is the trade's, not the market's. A short filled at 100 and
    closed at 90 captured +10 pips; taking `exit - entry` unsigned would report
    every profitable short as a loss and still add up in money."""
    bs = bars([(100, 101, 99, 100),
               (100, 101, 99, 100),      # short at 100, TP = 90
               (100, 101, 85, 100)])
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0,
                                    side=SignalType.ENTER_SHORT),
              cfg=_pips_cfg())
    row = res.ledger.iloc[0]
    assert row["exit_reason"] == EXIT_TP
    assert row["pips"] == pytest.approx(10.0)
    assert res.metrics["pips_won"] == pytest.approx(10.0)


def test_pips_are_gross_and_the_money_is_not():
    """The reason both are reported, in the one direction this engine can show
    it: a commission comes out of `net_pl` and cannot come out of a price
    distance, so the gap between the two numbers is what the trade paid.

    Commission and not spread, deliberately. This engine charges the spread by
    moving the FILL PRICE (see test_round_trip_costs_exactly_one_spread_not_two),
    so a spread is already inside both `gross_pl` and `pips` and would prove
    nothing here -- which is itself worth knowing before reading a pips column
    as "before costs".
    """
    bs = bars([(100, 101, 99, 100),
               (100, 101, 99, 100),
               (100, 115, 99, 100)])
    costs = CostModel(CostConfig(spread_source="none",
                                 commission_per_lot_round_turn=3.0), SPEC)
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0), costs=costs,
              cfg=_pips_cfg())
    row = res.ledger.iloc[0]
    # The full 10.00 target captured, and 3.00 of it handed to the broker.
    assert row["pips"] == pytest.approx(10.0)
    assert row["gross_pl"] == pytest.approx(10.0)
    assert row["net_pl"] == pytest.approx(7.0)


def test_a_run_with_no_pip_defined_reports_no_pips_rather_than_zero():
    """The default BacktestConfig has pip_size=0 -- an unconfigured symbol. Zero
    pips would read as a strategy that captured no movement, so the metrics are
    None and the report prints a dash."""
    bs = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 85, 100)])
    res = run(bs, EnterOnceStrategy(sl=10.0, tp=10.0))
    assert res.metrics["net_pips"] is None
    assert res.metrics["pips_won"] is None and res.metrics["pips_lost"] is None


def test_the_scaled_out_leg_is_NOT_part_of_the_pip_result():
    """Half banked at +5, the rest run to +10 -> 10 pips, not 7.5 and not 15.

    Pips are measured as though the position were the smallest lot a broker will
    take, and 0.01 lots cannot be scaled out -- so the distance is entry to
    FINAL exit and the banked leg contributes nothing. Same basis as the live
    trade fold, so a pip on the Backtest page and a pip on the Trade History
    page mean one thing.

    7.5 is what volume-weighting the two legs gave, and it is wrong for the job:
    it made the figure move with `cfg.volume`, which is the one thing a price
    distance is reported to be free of. 15 is what counting each leg in full
    would give -- a 100-pip target reported as 150.
    """
    class ScaleOut(EnterOnceStrategy):
        def on_bar(self, ctx):
            if ctx.index == 0 and ctx.position is None:
                return [Signal(SignalType.ENTER_LONG, "test", ctx.bar.close,
                               sl_distance=10.0, tp_distance=10.0,
                               be_trigger_distance=5.0, partial_fraction=0.5)]
            return []

    bs = bars([(100, 101, 99, 100),
               (100, 101, 99, 100),      # entry at 100
               (100, 106, 99, 100),      # trigger at 105 -> half out
               # The break-even stop is live from HERE, so this bar must not
               # revisit 100 -- it would scratch the runner before the target
               # and the distance would be measured to the wrong exit.
               (106, 115, 105, 112)])    # target at 110 -> the runner
    res = run(bs, ScaleOut(), cfg=_pips_cfg())
    row = res.ledger.iloc[0]
    assert row["partial_volume"] == pytest.approx(0.5)
    assert row["pips"] == pytest.approx(10.0)
    # And the same run at a size the broker cannot split reports the same
    # distance, which is the property the whole definition exists for: nothing
    # about `volume` reaches the pips column.
    small = run(bs, ScaleOut(), cfg=_pips_cfg(volume=0.01))
    assert small.ledger.iloc[0]["partial_volume"] == 0.0
    assert small.ledger.iloc[0]["pips"] == pytest.approx(10.0)


def test_the_research_default_matches_the_live_bot():
    """Both paths ship the centre-line exit OFF, and drift here is silent.

    The research stack is the honest engine, so it has to model the rules the
    bot actually runs -- a default that disagreed would make every report
    describe a configuration nothing trades. Note what this does NOT check: the
    live bot reads the flag from Postgres, so a dashboard toggle moves live
    without moving this. Pass --exit-at-mean to reproduce that in a report.
    """
    from backend.core.symbols import SYMBOL_CONFIG
    from backend.strategy.nw_envelope import NWConfig

    assert NWConfig().exit_at_mean is False
    for symbol, cfg in SYMBOL_CONFIG.items():
        # Keyed access, not .get: a symbol missing the key would fall through to
        # a default somewhere instead of stating its own rule.
        assert cfg["exit_at_mean"] is False, symbol
