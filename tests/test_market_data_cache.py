"""Data layer: cache round-trip, warm-up padding, and loud failure.

All offline -- no MT5, no network. The warm-up tests are the important ones:
they pin the fix for the bug where the backtest silently discarded the first
~998 bars of every requested window.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.core.errors import DataUnavailable
from backend.core.types import SymbolSpec
from backend.data.cache import (
    CachedMarketData, FileMarketData, read_shard, write_shard, write_spec,
)
from tests.fixtures.synthetic import make_ohlc

SPEC = SymbolSpec(name="XAUUSDm", digits=2, point=0.01, tick_size=0.01,
                  tick_value=1.0, contract_size=100.0)


@pytest.fixture
def cache_root(tmp_path):
    """A cache holding one month of M5 bars for XAUUSDm."""
    root = str(tmp_path / "data")
    write_spec(SPEC, root)
    df = make_ohlc("range", n=6000, seed=21)
    df.index = pd.date_range("2026-03-01", periods=len(df), freq="5min", tz="UTC")
    for month, part in df.groupby(df.index.strftime("%Y-%m")):
        write_shard(part, root, "XAUUSDm", "M5", month)
    return root, df


def test_shard_roundtrip_is_lossless(cache_root):
    root, df = cache_root
    back = read_shard(root, "XAUUSDm", "M5", "2026-03")
    first = df[df.index.strftime("%Y-%m") == "2026-03"]
    assert len(back) == len(first)
    pd.testing.assert_series_equal(
        back["close"].reset_index(drop=True),
        first["close"].reset_index(drop=True),
        check_exact=False, rtol=1e-12,
    )
    assert str(back.index.tz) == "UTC"


def test_warmup_bars_are_prepended_and_excluded_from_evaluation(cache_root):
    root, _ = cache_root
    md = FileMarketData(root)
    start = datetime(2026, 3, 10, tzinfo=timezone.utc)
    end = datetime(2026, 3, 12, tzinfo=timezone.utc)

    plain = md.get_bars("XAUUSDm", "M5", start, end, warmup_bars=0)
    padded = md.get_bars("XAUUSDm", "M5", start, end, warmup_bars=998)

    assert padded.warmup_count == 998
    assert len(padded.df) == len(plain.df) + 998
    # The evaluated window is identical either way -- warm-up only primes indicators.
    assert len(padded.eval_slice()) == len(plain.df)
    assert padded.eval_slice().index[0] >= start
    assert padded.df.index[0] < start
    assert not padded.warnings


def test_short_history_warns_instead_of_silently_producing_nan(cache_root):
    """The old failure mode was invisible: too little history -> all-NaN bands ->
    every comparison False -> the bot never trades and never says why."""
    root, _ = cache_root
    md = FileMarketData(root)
    start = datetime(2026, 3, 1, 0, 30, tzinfo=timezone.utc)  # only 6 bars precede
    bs = md.get_bars("XAUUSDm", "M5", start,
                     datetime(2026, 3, 2, tzinfo=timezone.utc), warmup_bars=998)
    assert bs.warmup_count < 998
    assert bs.warnings and "warm-up" in bs.warnings[0]


def test_missing_data_names_the_exact_snapshot_command(tmp_path):
    root = str(tmp_path / "empty")
    write_spec(SPEC, root)
    md = FileMarketData(root)
    with pytest.raises(DataUnavailable) as ei:
        md.get_bars("XAUUSDm", "M5",
                    datetime(2020, 1, 1, tzinfo=timezone.utc),
                    datetime(2020, 2, 1, tzinfo=timezone.utc))
    msg = str(ei.value)
    assert "backend.data.snapshot" in msg
    assert "--symbol XAUUSDm" in msg
    assert "MT5 HOST" in msg


def test_missing_spec_is_explicit(tmp_path):
    with pytest.raises(DataUnavailable) as ei:
        FileMarketData(str(tmp_path)).get_symbol_spec("NOPE")
    assert "--spec-only" in str(ei.value)


def test_offline_cache_never_touches_upstream(cache_root):
    root, _ = cache_root

    class Boom(object):
        def get_bars(self, *a, **k):
            raise AssertionError("upstream must not be called when offline=True")

        def get_symbol_spec(self, *a, **k):
            raise AssertionError("upstream must not be called when offline=True")

    md = CachedMarketData(upstream=Boom(), root=root, offline=True)
    bs = md.get_bars("XAUUSDm", "M5",
                     datetime(2026, 3, 10, tzinfo=timezone.utc),
                     datetime(2026, 3, 11, tzinfo=timezone.utc), warmup_bars=100)
    assert len(bs.eval_slice()) > 0
    assert bs.source == "cache"


def test_barset_meta_reports_what_was_actually_evaluated(cache_root):
    root, _ = cache_root
    bs = FileMarketData(root).get_bars(
        "XAUUSDm", "M5",
        datetime(2026, 3, 10, tzinfo=timezone.utc),
        datetime(2026, 3, 11, tzinfo=timezone.utc), warmup_bars=500)
    meta = bs.meta()
    assert meta["warmup_count"] == 500
    assert meta["bars_evaluated"] == meta["bars_total"] - 500
    assert meta["symbol"] == "XAUUSDm"
