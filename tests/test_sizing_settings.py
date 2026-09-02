"""The two lot numbers are now editable from the UI, so they need the same care
the rest of the live loop gets.

Two things are pinned here:

  * the lots <-> fraction boundary, because storing the UI's lot count instead of
    the fraction is the exact mistake SYMBOL_CONFIG warns about; and
  * `simulate_legacy` with the scale-out OFF still reproducing the original
    close-only engine trade for trade, so the rule that was added to POST
    /backtest can be shown to have changed nothing except what it was meant to.
"""

import json

import numpy as np
import pandas as pd
import pytest

from backend.core.errors import ConfigRejected
from tests.fixtures.synthetic import make_ohlc

mt5 = pytest.importorskip(
    "MetaTrader5", reason="bot_manager still imports MetaTrader5 (Phase 1.4 removes this)"
)

from backend import bot_manager as bm


BASE = {
    "pip": 0.1,
    "lot_size": 0.1,
    "sl_pips": 70,
    "tp_pips": 100,
    "profit_mult": 100,
    "be_trigger_pips": 50,
    "partial_fraction": 0.0,
}


def cfg(**over):
    out = dict(BASE)
    out.update(over)
    return out


# ---------------------------------------------------------------------------
# lots <-> fraction
# ---------------------------------------------------------------------------

def test_scale_out_is_stored_as_a_fraction_not_a_lot_count():
    # The whole point: 0.05 out of 0.1 is "half", and half is what survives a
    # later change of lot size. Storing 0.05 would silently become a quarter.
    assert bm.scale_out_fraction(0.1, 0.05) == pytest.approx(0.5)
    assert bm.scale_out_fraction(0.2, 0.05) == pytest.approx(0.25)
    assert bm.scale_out_fraction(0.1, 0.0) == 0.0


@pytest.mark.parametrize("lot,out", [(0.1, 0.1), (0.1, 0.2), (0.0, 0.0), (-0.1, 0.05)])
def test_scale_out_rejects_sizes_that_are_not_a_partial(lot, out):
    # >= lot_size is not a scale-out, it is a full exit at the trigger -- and
    # NWConfig requires partial_fraction < 1 for the same reason.
    with pytest.raises(ConfigRejected):
        bm.scale_out_fraction(lot, out)


def test_scale_out_rejects_negative_and_non_finite():
    with pytest.raises(ConfigRejected):
        bm.scale_out_fraction(0.1, -0.01)
    with pytest.raises(ConfigRejected):
        bm.scale_out_fraction(float("nan"), 0.05)
    with pytest.raises(ConfigRejected):
        bm.scale_out_fraction(float("inf"), 0.05)


def test_split_lots_reports_what_the_broker_can_actually_execute():
    assert bm._split_lots(0.1, 0.5, 0.01, 0.01) == (0.05, 0.05)
    # 0.01 lots cannot be halved anywhere: nothing to bank, whole size runs on.
    assert bm._split_lots(0.01, 0.5, 0.01, 0.01) == (0.0, 0.01)
    # ...nor can the remainder be allowed to fall under the broker minimum.
    assert bm._split_lots(0.02, 0.9, 0.01, 0.01) == (0.0, 0.02)
    assert bm._split_lots(0.1, 0.0, 0.01, 0.01) == (0.0, 0.1)


def test_split_lots_snaps_to_the_volume_step():
    # 0.03 * 0.5 = 0.015, which no 0.01-step broker will accept.
    out, runner = bm._split_lots(0.03, 0.5, 0.01, 0.01)
    assert (out, runner) == (0.02, 0.01)


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def test_saved_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "lot_size", 0.03)
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "partial_fraction", 0.25)
    bm._save_settings()

    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved == {"XAUUSDm": {"lot_size": 0.03, "partial_fraction": 0.25}}

    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "lot_size", 0.1)
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "partial_fraction", 0.5)
    bm._load_settings()
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["lot_size"] == 0.03
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["partial_fraction"] == 0.25


def test_settings_file_cannot_introduce_a_symbol_or_move_a_stop(tmp_path, monkeypatch):
    """A file on disk gets to change the two editable numbers and nothing else.

    Anything wider would let a stray settings.json widen a stop or add an
    unbacktested symbol, neither of which the UI can do.
    """
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "XAUUSDm": {"lot_size": 0.02, "sl_pips": 5, "profit_mult": 999},
        "EURUSDm": {"lot_size": 5.0},
    }))
    monkeypatch.setattr(bm, "SETTINGS_FILE", str(path))
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "lot_size", 0.1)

    bm._load_settings()

    assert bm.SYMBOL_CONFIG["XAUUSDm"]["lot_size"] == 0.02      # editable
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["sl_pips"] == 70         # not editable
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["profit_mult"] == 100    # not editable
    assert "EURUSDm" not in bm.SYMBOL_CONFIG


def test_out_of_range_saved_values_are_ignored_not_applied(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"XAUUSDm": {"lot_size": -1.0, "partial_fraction": 1.0}}))
    monkeypatch.setattr(bm, "SETTINGS_FILE", str(path))
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "lot_size", 0.1)
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "partial_fraction", 0.5)

    bm._load_settings()

    assert bm.SYMBOL_CONFIG["XAUUSDm"]["lot_size"] == 0.1
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["partial_fraction"] == 0.5


def test_corrupt_settings_file_falls_back_to_the_defaults(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    monkeypatch.setattr(bm, "SETTINGS_FILE", str(path))
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "lot_size", 0.1)

    bm._load_settings()  # must not raise -- a bad file cannot stop the API booting

    assert bm.SYMBOL_CONFIG["XAUUSDm"]["lot_size"] == 0.1


# ---------------------------------------------------------------------------
# simulate_legacy
# ---------------------------------------------------------------------------

def _original_engine(df, outs, uppers, lowers, config, initial_balance):
    """Verbatim copy of run_backtest's loop before the scale-out was added.

    Pinned rather than trusted, exactly as tests/test_legacy_regression.py pins
    the envelope refactor: adding a rule to an engine is only safe to ship if the
    engine with the rule turned off still produces the old numbers.
    """
    balance = initial_balance
    trades_opened = 0
    wins = 0
    losses = 0
    total_pl = 0.0
    max_drawdown = 0.0
    peak_balance = initial_balance

    position = None
    entry_price = 0.0

    pip = config["pip"]
    sl_pips = config["sl_pips"] * pip
    tp_pips = config["tp_pips"] * pip
    lot_size = config["lot_size"]
    profit_mult = config["profit_mult"]

    for i in range(len(df)):
        price = df["close"].iloc[i]
        out = outs[i]
        upper = uppers[i]
        lower = lowers[i]

        if np.isnan(out) or np.isnan(upper) or np.isnan(lower):
            continue

        if position is None:
            if price < lower:
                position = "BUY"
                entry_price = price
                trades_opened += 1
            elif price > upper:
                position = "SELL"
                entry_price = price
                trades_opened += 1
        elif position == "BUY":
            if price >= entry_price + tp_pips or price <= entry_price - sl_pips or price >= out:
                exit_price = price
                if price <= entry_price - sl_pips:
                    exit_price = entry_price - sl_pips
                elif price >= entry_price + tp_pips:
                    exit_price = entry_price + tp_pips
                pl = (exit_price - entry_price) * lot_size * profit_mult
                balance += pl
                total_pl += pl
                if pl > 0:
                    wins += 1
                elif pl < 0:
                    losses += 1
                position = None
        elif position == "SELL":
            if price <= entry_price - tp_pips or price >= entry_price + sl_pips or price <= out:
                exit_price = price
                if price >= entry_price + sl_pips:
                    exit_price = entry_price + sl_pips
                elif price <= entry_price - tp_pips:
                    exit_price = entry_price - tp_pips
                pl = (entry_price - exit_price) * lot_size * profit_mult
                balance += pl
                total_pl += pl
                if pl > 0:
                    wins += 1
                elif pl < 0:
                    losses += 1
                position = None

        peak_balance = max(peak_balance, balance)
        drawdown = (peak_balance - balance) / peak_balance * 100 if peak_balance != 0 else 0
        max_drawdown = max(max_drawdown, drawdown)

    return {
        "final_balance": balance,
        "total_pl": total_pl,
        "trades_opened": trades_opened,
        "wins": wins,
        "losses": losses,
        "max_drawdown": max_drawdown,
    }


def _envelope(df):
    bot = bm.TradingBot("XAUUSDm")
    return bot.calculate_envelope(df)


@pytest.mark.parametrize("kind", ["trend", "range", "spike"])
def test_scale_out_off_reproduces_the_original_engine(kind):
    df = make_ohlc(kind, n=1600, seed=5)
    outs, uppers, lowers = _envelope(df)
    config = cfg(partial_fraction=0.0)

    ref = _original_engine(df, outs, uppers, lowers, config, 1000.0)
    got = bm.simulate_legacy(df, outs, uppers, lowers, config, 1000.0)

    for key in ref:
        assert got[key] == pytest.approx(ref[key], rel=1e-12, abs=1e-9), key
    assert got["partials_fired"] == 0
    assert got["partial_pl"] == 0.0


@pytest.mark.parametrize("kind", ["trend", "range", "spike"])
def test_be_trigger_of_zero_also_reproduces_the_original(kind):
    # Two independent off-switches, matching manage_position()'s guard.
    df = make_ohlc(kind, n=1600, seed=5)
    outs, uppers, lowers = _envelope(df)
    ref = bm.simulate_legacy(df, outs, uppers, lowers, cfg(partial_fraction=0.0), 1000.0)
    got = bm.simulate_legacy(
        df, outs, uppers, lowers,
        cfg(partial_fraction=0.5, be_trigger_pips=0), 1000.0)
    assert got["total_pl"] == pytest.approx(ref["total_pl"])
    assert got["partials_fired"] == 0


def test_scale_out_banks_at_the_trigger_and_pulls_the_stop_to_entry():
    """One long: up through the trigger, then all the way back to the old stop.

    Without the rule that is a full 70-pip loss. With it, half is banked at +5.00
    and the runner exits flat at entry -- which is the whole shape of the rule,
    and the reason it clips winners as well as losers.
    """
    config = cfg(partial_fraction=0.5)
    close = [2000.0, 1990.0, 1996.0, 1990.0, 1990.0]
    df = pd.DataFrame({"close": close})
    n = len(close)
    outs = np.full(n, 2050.0)      # never reached -> no mean-reversion exit
    uppers = np.full(n, 2100.0)
    lowers = np.array([1900.0, 1995.0, 1900.0, 1900.0, 1900.0])  # bar 1 enters long

    res = bm.simulate_legacy(df, outs, uppers, lowers, config, 1000.0)

    assert res["trades_opened"] == 1
    assert res["partials_fired"] == 1
    assert res["scale_out_lots"] == 0.05
    # Bar 2 closes at +6.00, past the 5.00 trigger; this engine fills the trigger
    # AT its level rather than at the close, so +5.00 is what gets banked.
    assert res["partial_pl"] == pytest.approx(5.00 * 0.05 * 100)
    # ...and the runner exits at entry, not at entry - 7.00.
    assert res["total_pl"] == pytest.approx(res["partial_pl"])
    assert res["wins"] == 1


def test_without_the_rule_the_same_path_is_a_full_stop_out():
    config = cfg(partial_fraction=0.0)
    close = [2000.0, 1990.0, 1996.0, 1982.0, 1982.0]
    df = pd.DataFrame({"close": close})
    n = len(close)
    outs = np.full(n, 2050.0)
    uppers = np.full(n, 2100.0)
    lowers = np.array([1900.0, 1995.0, 1900.0, 1900.0, 1900.0])

    res = bm.simulate_legacy(df, outs, uppers, lowers, config, 1000.0)

    assert res["partials_fired"] == 0
    assert res["total_pl"] == pytest.approx(-7.00 * 0.1 * 100)   # full 70-pip stop
    assert res["losses"] == 1


def test_a_trade_is_scored_once_even_when_it_scales_out():
    """The partial is not its own win.

    Counting a banked partial as a separate win is exactly how a scale-out comes
    to look like free hit rate; the ledger in backend/backtest/ scores one result
    per trade and this engine now agrees with it.
    """
    config = cfg(partial_fraction=0.5)
    close = [2000.0, 1990.0, 1996.0, 1990.0, 1990.0]
    df = pd.DataFrame({"close": close})
    n = len(close)
    outs = np.full(n, 2050.0)
    uppers = np.full(n, 2100.0)
    lowers = np.array([1900.0, 1995.0, 1900.0, 1900.0, 1900.0])

    res = bm.simulate_legacy(df, outs, uppers, lowers, config, 1000.0)
    assert res["wins"] + res["losses"] == res["trades_opened"] == 1


def test_a_size_too_small_to_split_still_gets_break_even():
    """Mirrors manage_position(): 0.01 lots banks nothing but keeps the stop move."""
    config = cfg(lot_size=0.01, partial_fraction=0.5)
    close = [2000.0, 1990.0, 1996.0, 1982.0, 1982.0]
    df = pd.DataFrame({"close": close})
    n = len(close)
    outs = np.full(n, 2050.0)
    uppers = np.full(n, 2100.0)
    lowers = np.array([1900.0, 1995.0, 1900.0, 1900.0, 1900.0])

    res = bm.simulate_legacy(df, outs, uppers, lowers, config, 1000.0)

    assert res["scale_out_lots"] == 0.0
    assert res["partials_fired"] == 0
    assert res["runner_lots"] == 0.01
    assert res["total_pl"] == pytest.approx(0.0)   # stopped at entry, not at -7.00


def test_lot_size_scales_pl_linearly():
    df = make_ohlc("range", n=1600, seed=5)
    outs, uppers, lowers = _envelope(df)
    small = bm.simulate_legacy(df, outs, uppers, lowers,
                               cfg(lot_size=0.1, partial_fraction=0.0), 1000.0)
    big = bm.simulate_legacy(df, outs, uppers, lowers,
                             cfg(lot_size=0.2, partial_fraction=0.0), 1000.0)
    assert big["total_pl"] == pytest.approx(small["total_pl"] * 2)
    assert big["trades_opened"] == small["trades_opened"]
