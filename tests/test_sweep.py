"""The sweep harness, and the two properties that make its results readable.

`Trading Bot.md` section 11 is the whole reason this file exists: a harness that
can quietly express a per-symbol fit, or that ranks a lucky cell above a stable
region, produces exactly the overfitting it was built to prevent.
"""

import numpy as np
import pandas as pd
import pytest

from backend.core.symbols import SYMBOL_CONFIG
from backend.scripts import sweep


# --- the shared-pip-count constraint, made structural -----------------------

def test_the_grid_cannot_express_different_pip_counts_per_symbol():
    """One cell, both symbols: the PIP COUNTS are shared and only the price
    distances differ, because each symbol multiplies them through its own pip.

    This is the constraint that makes "one rule, two instruments" true. If a
    grid key could ever be per-symbol, two instruments could be fitted
    separately while still looking like one configuration in the report.
    """
    cell = {"sl_pips": 70, "tp_pips": 100, "be_trigger_pips": 50,
            "partial_fraction": 0.5}
    gold = sweep.build_config(cell, "XAUUSDm")
    btc = sweep.build_config(cell, "BTCUSDm")

    assert gold.sl_price == pytest.approx(7.0)      # 70 x 0.1
    assert btc.sl_price == pytest.approx(700.0)     # 70 x 10.0
    assert gold.tp_price == pytest.approx(10.0)
    assert btc.tp_price == pytest.approx(1000.0)
    # Same counts, different money -- which is the point.
    assert btc.sl_price / gold.sl_price == pytest.approx(
        SYMBOL_CONFIG["BTCUSDm"]["pip"] / SYMBOL_CONFIG["XAUUSDm"]["pip"])

    # And there is no key through which a caller could ask for two counts.
    assert "symbol" not in sweep.GRID_KEYS
    for key in sweep.PIP_KEYS:
        assert key in sweep.GRID_KEYS


def test_an_unknown_sweep_parameter_is_refused_not_ignored():
    """A typo'd --param that is silently dropped spends an hour of compute
    sweeping nothing, and the CSV looks exactly like a real result."""
    args = _Args(param=["sl_pipz=70,80"])
    with pytest.raises(SystemExit) as exc:
        sweep.parse_grid(args)
    assert "sl_pipz" in str(exc.value)


def test_a_zero_scale_out_disables_the_rule_rather_than_arming_it_at_zero():
    cfg = sweep.build_config(
        {"sl_pips": 70, "tp_pips": 100, "be_trigger_pips": 50,
         "partial_fraction": 0.0}, "XAUUSDm")
    assert cfg.be_trigger_mode == "none"


# --- the objective ----------------------------------------------------------

def test_the_objective_is_a_t_statistic_so_a_tiny_sample_cannot_win():
    """Two cells with the SAME expectancy but different sample sizes must not
    rank equally -- that is how a spectacular result on twelve trades wins a
    sweep."""
    solid = {"expectancy_r": 0.05, "expectancy_r_se": 0.01}     # n large
    lucky = {"expectancy_r": 0.05, "expectancy_r_se": 0.20}     # n tiny
    assert sweep.objective(solid) > sweep.objective(lucky)
    assert sweep.objective(solid) == pytest.approx(5.0)


def test_an_unmeasurable_objective_is_nan_not_zero():
    assert np.isnan(sweep.objective({"expectancy_r": 0.1, "expectancy_r_se": 0.0}))
    assert np.isnan(sweep.objective({}))


# --- neighbourhood stability ------------------------------------------------

def _grid_frame(objectives):
    return pd.DataFrame({
        "symbol": "XAUUSDm", "timeframe": "M5", "scenario": "central",
        "split": "train",
        "sl_pips": [50, 60, 70, 80, 90],
        "objective": objectives,
    })


def test_a_spike_is_scored_below_its_own_value_and_flagged():
    """The `RSI = 53` case: one cell far above its neighbours is noise, and the
    harness has to make that visible rather than rank it first."""
    df = sweep.add_neighbourhood(_grid_frame([0.1, 0.1, 3.0, 0.1, 0.1]),
                                 ["sl_pips"])
    spike = df[df.sl_pips == 70].iloc[0]
    assert spike.objective == 3.0
    assert spike.nbhd_median < spike.objective, "must not be ranked at its own value"
    assert spike.spike_ratio > 2.0, "the ratio is what makes a spike legible"


def test_a_plateau_keeps_its_value():
    df = sweep.add_neighbourhood(_grid_frame([1.0, 1.0, 1.0, 1.0, 1.0]),
                                 ["sl_pips"])
    row = df[df.sl_pips == 70].iloc[0]
    assert row.nbhd_median == pytest.approx(1.0)
    assert row.spike_ratio == pytest.approx(1.0)
    assert row.nbhd_std == pytest.approx(0.0)


def test_edge_cells_report_fewer_neighbours_so_they_can_be_gated_out():
    """A cell at the edge of the grid has one neighbour, so its "plateau" is
    half-measured. `--min-neighbours` exists to drop it; that only works if the
    count is honest."""
    df = sweep.add_neighbourhood(_grid_frame([1.0, 1.0, 1.0, 1.0, 5.0]),
                                 ["sl_pips"])
    assert int(df[df.sl_pips == 90].iloc[0].n_neighbours) == 1
    assert int(df[df.sl_pips == 70].iloc[0].n_neighbours) == 2


def test_symbols_are_not_each_others_neighbours():
    """Neighbourhood smoothing must never average gold against Bitcoin: that
    would hide the per-instrument difference the sweep exists to measure."""
    df = pd.DataFrame({
        "symbol": ["XAUUSDm"] * 3 + ["BTCUSDm"] * 3,
        "timeframe": "M5", "scenario": "central", "split": "train",
        "sl_pips": [70, 90, 110] * 2,
        "objective": [1.0, 1.0, 1.0, 9.0, 9.0, 9.0],
    })
    out = sweep.add_neighbourhood(df, ["sl_pips"])
    gold = out[out.symbol == "XAUUSDm"]
    assert gold.nbhd_median.max() == pytest.approx(1.0)
    assert out[out.symbol == "BTCUSDm"].nbhd_median.min() == pytest.approx(9.0)


class _Args(object):
    def __init__(self, grid=None, param=None):
        self.grid = grid
        self.param = param
