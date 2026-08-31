"""Nadaraya-Watson envelope (non-repainting "endpoint" method).

Port of the LuxAlgo Pine indicator's *endpoint* branch -- see
``LuxAlgo - Nadaraya-Watson Envelope.pine`` lines 41-57. Only the repaint=false
path is implemented; the repainting branch fits a centred kernel over the whole
window and would leak future bars into every historical value.

Two implementation traps, both easy to reintroduce:

1. **The kernel is not reversed.** ``np.convolve(src, k)[j] == sum(src[j-m]*k[m])``,
   which is already the endpoint form with ``k[0] = 1`` on the current bar. The
   ``weights[::-1]`` in the original loop was correct only because it was paired
   with a forward-ordered window slice. Reversing the kernel here instead
   produces a silently *repainting-shaped* series that still looks plausible.

2. **The denominator is always the full-window weight sum**, even when the kernel
   is truncated. Truncation drops negligible tail terms; it does not renormalise.
   Renormalising introduces a bias that grows with trend slope.

Truncation note: at the default ``bandwidth=8`` the Gaussian weights fall to
~3.3e-09 by tap 50, and the first 60 taps carry 100% of the total weight to
float64 precision. ``taps`` is therefore a pure speed/warm-up knob, not a
parameter to tune. The default (None = full window) is exact.
"""

from typing import NamedTuple, Optional

import numpy as np
import pandas as pd


class NWEnvelope(NamedTuple):
    out: np.ndarray      # kernel-smoothed centre line
    mae: np.ndarray      # band half-width (already multiplied by `mult`)
    upper: np.ndarray
    lower: np.ndarray


def gaussian_kernel(taps, bandwidth):
    # type: (int, float) -> np.ndarray
    """Weights exp(-(i^2 / (2h^2))) for i in 0..taps-1; index 0 is the newest bar."""
    if taps <= 0:
        raise ValueError("taps must be positive")
    if bandwidth <= 0:
        raise ValueError("bandwidth must be positive")
    i = np.arange(taps, dtype=np.float64)
    return np.exp(-(i ** 2) / (2.0 * bandwidth ** 2))


def nw_warmup_bars(window=500, mae_window=499, taps=None):
    # type: (int, int, Optional[int]) -> int
    """Bars of history needed before the FIRST finite `upper`/`lower` value.

    Equivalently: the index of the first finite `upper`/`lower`. `out` first
    becomes valid at index (effective_window - 1); the rolling MAE then needs
    (mae_window - 1) further bars on top of that.

    This is the number the data layer must pad with, and the reason the live bot
    silently stopped trading: it fetched WINDOW_SIZE*2 = 1000 bars against a
    warm-up of 998, leaving two usable bars.
    """
    effective = window if taps is None else min(taps, window)
    return (effective - 1) + (mae_window - 1)


def nw_endpoint(src, bandwidth=8.0, window=500, taps=None):
    # type: (np.ndarray, float, int, Optional[int]) -> np.ndarray
    """Vectorised kernel-smoothed centre line. Causal: bar j uses only bars <= j."""
    src = np.asarray(src, dtype=np.float64)
    k_full = gaussian_kernel(window, bandwidth)
    denom = k_full.sum()                       # ALWAYS the full-window sum
    k = k_full if taps is None else k_full[:min(taps, window)]

    out = np.convolve(src, k)[:len(src)] / denom   # NOT k[::-1] -- see module docstring
    out[: len(k) - 1] = np.nan                     # incomplete window
    return out


def nw_envelope(src, bandwidth=8.0, mult=3.0, window=500, mae_window=499, taps=None):
    # type: (np.ndarray, float, float, int, int, Optional[int]) -> NWEnvelope
    """Centre line plus a mean-absolute-error band.

    `mae_window` defaults to 499 to match Pine's ``ta.sma(abs(src-out), 499)``.
    The original Python used ``rolling(500)``, an undocumented off-by-one; pass
    ``mae_window=500`` to reproduce that exactly.
    """
    src = np.asarray(src, dtype=np.float64)
    out = nw_endpoint(src, bandwidth=bandwidth, window=window, taps=taps)
    dev = pd.Series(np.abs(src - out))
    mae = dev.rolling(mae_window, min_periods=mae_window).mean().values * mult
    return NWEnvelope(out=out, mae=mae, upper=out + mae, lower=out - mae)


def _nw_endpoint_naive(src, bandwidth=8.0, window=500):
    # type: (np.ndarray, float, int) -> np.ndarray
    """Reference implementation: the original explicit loop. Tests only.

    Kept byte-for-byte faithful to the pre-refactor `calculate_envelope` so the
    vectorised version can be proven equivalent rather than assumed to be.
    """
    src = np.asarray(src, dtype=np.float64)
    weights = np.exp(-(np.arange(window) ** 2 / (2 * bandwidth ** 2)))
    sum_weights = np.sum(weights)
    out = np.full(len(src), np.nan)
    for j in range(window - 1, len(src)):
        win = src[j - window + 1: j + 1]
        out[j] = np.sum(win * weights[::-1]) / sum_weights
    return out
