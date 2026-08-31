"""Proves the refactor changed no numbers.

`calculate_envelope` was rewritten from two redundant O(n*500) Python loops into
a single convolution. That is a large change to the one function every signal
depends on, so it is pinned against a byte-faithful copy of the original code
rather than trusted.
"""

import numpy as np
import pytest

from tests.fixtures.synthetic import make_ohlc

mt5 = pytest.importorskip(
    "MetaTrader5", reason="bot_manager still imports MetaTrader5 (Phase 1.4 removes this)"
)

from backend.bot_manager import BANDWIDTH, MAE_WINDOW, MULT, WINDOW_SIZE, TradingBot


def _legacy_calculate_envelope(df):
    """Verbatim copy of the pre-refactor implementation (commit a595084)."""
    close = df["close"].values
    src = close
    h = BANDWIDTH
    i_vals = np.arange(WINDOW_SIZE)
    weights = np.exp(-(i_vals ** 2 / (2 * h ** 2)))
    sum_weights = np.sum(weights)

    outs = np.full(len(src), np.nan)
    for j in range(WINDOW_SIZE - 1, len(src)):
        window = src[j - WINDOW_SIZE + 1: j + 1]
        outs[j] = np.sum(window * weights[::-1]) / sum_weights

    diffs = np.full(len(src), np.nan)
    for j in range(WINDOW_SIZE - 1, len(src)):
        window = src[j - WINDOW_SIZE + 1: j + 1]
        val = np.sum(window * weights[::-1]) / sum_weights
        diffs[j] = abs(src[j] - val)

    import pandas as pd

    mae = pd.Series(diffs).rolling(window=WINDOW_SIZE).mean().values * MULT
    return outs, outs + mae, outs - mae


@pytest.mark.parametrize("kind", ["trend", "range", "spike"])
def test_refactored_envelope_matches_original(kind):
    df = make_ohlc(kind, n=1400, seed=11)
    bot = TradingBot("XAUUSDm")

    ref_out, ref_up, ref_lo = _legacy_calculate_envelope(df)
    got_out, got_up, got_lo = bot.calculate_envelope(df)

    for name, ref, got in (
        ("out", ref_out, got_out),
        ("upper", ref_up, got_up),
        ("lower", ref_lo, got_lo),
    ):
        np.testing.assert_array_equal(
            np.isnan(ref), np.isnan(got), err_msg="%s NaN mask differs" % name
        )
        m = ~np.isnan(ref)
        np.testing.assert_allclose(got[m], ref[m], rtol=0, atol=1e-9,
                                   err_msg="%s values differ" % name)


def test_legacy_mae_window_is_500_not_pine_499():
    """The original used rolling(500); Pine uses 499. Keep it deliberate."""
    assert MAE_WINDOW == 500
