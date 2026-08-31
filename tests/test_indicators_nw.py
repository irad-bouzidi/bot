"""Indicator correctness.

These are the tests that must pass on a machine with NO MetaTrader5 terminal --
which is the whole point of Phase 1. They pin three things that were previously
unverifiable: numerical parity with the original loop, causality (no look-ahead),
and the warm-up length that silently disabled the live bot.
"""

import subprocess
import sys

import numpy as np
import pytest

from backend.indicators.nadaraya_watson import (
    NWEnvelope,
    _nw_endpoint_naive,
    gaussian_kernel,
    nw_endpoint,
    nw_envelope,
    nw_warmup_bars,
)
from tests.fixtures.synthetic import KINDS, make_ohlc


@pytest.mark.parametrize("kind", KINDS)
def test_matches_naive_reference(kind):
    """The vectorised path must equal the original explicit loop."""
    src = make_ohlc(kind, n=5000, seed=1)["close"].values
    ref = _nw_endpoint_naive(src)
    got = nw_endpoint(src)
    mask = ~np.isnan(ref)
    assert mask.any()
    np.testing.assert_allclose(got[mask], ref[mask], rtol=0, atol=1e-9)
    # NaN warm-up must line up too, not just the finite values.
    np.testing.assert_array_equal(np.isnan(ref), np.isnan(got))


def test_taps_80_is_within_float_noise():
    """The opt-in fast path must be exact for practical purposes."""
    src = make_ohlc("range", n=5000, seed=2)["close"].values
    ref = _nw_endpoint_naive(src)
    got = nw_endpoint(src, taps=80)
    mask = ~np.isnan(ref) & ~np.isnan(got)
    np.testing.assert_allclose(got[mask], ref[mask], rtol=0, atol=1e-9)


def test_taps_50_is_not_accurate_enough():
    """Documents WHY the default is the full kernel.

    At taps=50 the error reaches ~1e-6 in price units -- enough to flip a
    `close < lower` comparison for a bar sitting on the band.
    """
    src = make_ohlc("range", n=5000, seed=2)["close"].values
    ref = _nw_endpoint_naive(src)
    got = nw_endpoint(src, taps=50)
    mask = ~np.isnan(ref) & ~np.isnan(got)
    assert np.max(np.abs(got[mask] - ref[mask])) > 1e-9


@pytest.mark.parametrize("kind", ["trend", "range", "spike"])
def test_no_lookahead(kind):
    """Truncating the input must not change any earlier value.

    This is the property that fails loudly if anyone swaps in a centred window
    (mode="same") or the repainting branch of the Pine source.
    """
    df = make_ohlc(kind, n=2500, seed=3)
    src = df["close"].values
    full = nw_envelope(src)
    rs = np.random.RandomState(0)
    for i in rs.randint(1100, len(src), size=20):
        partial = nw_envelope(src[: i + 1])
        for a, b in ((partial.out[-1], full.out[i]), (partial.upper[-1], full.upper[i])):
            if np.isnan(a) and np.isnan(b):
                continue
            assert a == pytest.approx(b, abs=1e-12), "look-ahead at index %d" % i


def test_warmup_length_is_998_for_pine_window():
    """Encodes the bug that silently disabled the live bot.

    `out` first goes finite at index 499; the rolling MAE needs 498 more bars, so
    the first finite band value sits at index 997 (Pine's 499) or 998 (the
    original code's off-by-one rolling(500)). The live loop fetched
    WINDOW_SIZE*2 = 1000 bars against a warm-up of 998, leaving exactly TWO
    usable bars -- and none at all if the broker returned fewer than 999.
    """
    assert nw_warmup_bars(window=500, mae_window=499) == 997
    assert nw_warmup_bars(window=500, mae_window=500) == 998
    assert nw_warmup_bars(window=500, mae_window=499, taps=80) == 577

    src = make_ohlc("range", n=1200, seed=4)["close"].values
    for mae_window in (499, 500):
        w = nw_warmup_bars(window=500, mae_window=mae_window)
        env = nw_envelope(src, mae_window=mae_window)
        assert np.isnan(env.upper[:w]).all(), "finite value before warm-up ends"
        assert np.isfinite(env.upper[w]), "no finite value at the warm-up boundary"


def test_too_few_bars_yields_all_nan_not_a_silent_zero():
    """Under-length input must be detectably NaN, never a plausible number."""
    src = make_ohlc("range", n=900, seed=5)["close"].values
    env = nw_envelope(src)
    assert np.isnan(env.upper).all()
    assert np.isnan(env.lower).all()


def test_kernel_is_causal_and_peaks_on_current_bar():
    k = gaussian_kernel(500, 8.0)
    assert k[0] == 1.0
    assert np.all(np.diff(k) <= 0)
    assert k[50] < 1e-8


def test_envelope_band_ordering():
    src = make_ohlc("range", n=1500, seed=6)["close"].values
    env = nw_envelope(src)
    assert isinstance(env, NWEnvelope)
    m = ~np.isnan(env.upper)
    assert np.all(env.lower[m] <= env.out[m])
    assert np.all(env.out[m] <= env.upper[m])


def test_importable_without_metatrader5():
    """The Phase 1 gate: the indicator must import with MT5 unavailable."""
    code = (
        "import sys;"
        "sys.modules['MetaTrader5']=None;"
        "import backend.indicators.nadaraya_watson as m;"
        "import numpy as np;"
        "print(float(np.nansum(m.nw_endpoint(np.arange(1500,dtype=float)))))"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()
