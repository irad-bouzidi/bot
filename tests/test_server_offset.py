"""The server<->UTC offset measurement, and the stale-tick corruption it caused.

`mt5_source` imports MetaTrader5 at module scope, so these tests install a stub
first. That keeps the suite runnable with no terminal -- the same rule
`test_importable_without_metatrader5` guards for the indicator.
"""

import sys
import types

import pytest

TF = dict(TIMEFRAME_M1=1, TIMEFRAME_M5=5, TIMEFRAME_M15=15, TIMEFRAME_M30=30,
          TIMEFRAME_H1=16385, TIMEFRAME_H4=16388, TIMEFRAME_D1=16408)


class _Tick(object):
    def __init__(self, t):
        self.time = t


@pytest.fixture
def mt5_stub(monkeypatch):
    stub = types.ModuleType("MetaTrader5")
    for k, v in TF.items():
        setattr(stub, k, v)
    stub.ticks = {}
    stub.symbol_info_tick = lambda s: stub.ticks.get(s)
    monkeypatch.setitem(sys.modules, "MetaTrader5", stub)
    for name in [m for m in sys.modules if m.startswith("backend.data.mt5_source")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    import backend.data.mt5_source as src
    monkeypatch.setattr(src, "mt5", stub)
    monkeypatch.setattr(src._time, "time", lambda: 1_000_000.0)
    return stub, src


def test_a_live_tick_gives_the_offset_snapped_to_15_minutes(mt5_stub):
    stub, src = mt5_stub
    stub.ticks["XAUUSDm"] = _Tick(1_000_000 + 7200 + 100)   # +2h and a bit
    assert src.measure_server_utc_offset("XAUUSDm") == 7200


def test_a_stale_tick_is_refused_rather_than_read_as_a_distant_server(mt5_stub):
    """Gold on a Sunday: the newest tick is Friday's close, so `tick.time - now()`
    reads about -41 hours. Trusting it stamps every captured bar 41 hours out --
    which is exactly what happened to data/specs/XAUUSDm.json and to a whole H1
    capture, and why the sidecar had been marked "offset-repaired" by hand.

    -148500 is the literal value that corruption wrote."""
    stub, src = mt5_stub
    stub.ticks["XAUUSDm"] = _Tick(1_000_000 - 148_500)
    assert src._tick_offset("XAUUSDm") is None
    # Nothing else is ticking either, so the STORED value stands.
    assert src.measure_server_utc_offset("XAUUSDm", fallback=0) == 0
    assert src.measure_server_utc_offset("XAUUSDm", fallback=7200) == 7200


def test_a_shut_market_borrows_the_clock_from_a_symbol_that_is_still_ticking(mt5_stub):
    """Both symbols sit on ONE server clock, and BTCUSDm trades 24/7. So a gold
    snapshot on a Sunday can still measure the offset correctly -- which is what
    turns "cannot measure" from a corruption into a non-event."""
    stub, src = mt5_stub
    stub.ticks["XAUUSDm"] = _Tick(1_000_000 - 148_500)   # stale: market shut
    stub.ticks["BTCUSDm"] = _Tick(1_000_000 + 10_800)    # live: +3h
    assert src._tick_offset("XAUUSDm") is None
    assert src.measure_server_utc_offset("XAUUSDm") == 10_800


def test_no_tick_at_all_falls_back_to_the_stored_offset_never_to_a_guess(mt5_stub):
    stub, src = mt5_stub
    assert src.measure_server_utc_offset("XAUUSDm", fallback=7200) == 7200
    assert src.measure_server_utc_offset("XAUUSDm") == 0
