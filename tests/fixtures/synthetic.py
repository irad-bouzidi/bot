"""Deterministic OHLC fixtures. No MT5, no network, no cached data."""

import numpy as np
import pandas as pd

KINDS = ("trend", "range", "gap", "spike", "flat")


def make_ohlc(kind="range", n=3000, seed=0, start_price=2000.0, tf_seconds=300):
    """Generate a plausible OHLC frame with a `spread` column.

    Guarantees low <= min(open, close) <= max(open, close) <= high, which several
    engine invariants rely on.
    """
    rs = np.random.RandomState(seed)
    step = rs.randn(n) * 0.05

    if kind == "trend":
        step += 0.02
    elif kind == "range":
        pass
    elif kind == "flat":
        step = np.zeros(n)
    elif kind == "spike":
        step[n // 2] += 15.0
    elif kind == "gap":
        step[n // 3] += 8.0
        step[2 * n // 3] -= 8.0
    else:
        raise ValueError("unknown kind: %r" % (kind,))

    close = start_price + np.cumsum(step)
    open_ = np.concatenate([[start_price], close[:-1]])
    body_hi = np.maximum(open_, close)
    body_lo = np.minimum(open_, close)
    high = body_hi + np.abs(rs.randn(n)) * 0.03
    low = body_lo - np.abs(rs.randn(n)) * 0.03

    t = pd.to_datetime(
        np.arange(n) * tf_seconds + 1_700_000_000, unit="s", utc=True
    )
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "tick_volume": rs.randint(10, 500, n),
            "spread": rs.randint(15, 40, n),
            "real_volume": np.zeros(n, dtype=np.int64),
        },
        index=pd.DatetimeIndex(t, name="time"),
    )
