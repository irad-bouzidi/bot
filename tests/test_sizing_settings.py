"""The two lot numbers are now editable from the UI, so they need the same care
the rest of the live loop gets.

Two things are pinned here:

  * the lots <-> fraction boundary, because storing the UI's lot count instead of
    the fraction is the exact mistake SYMBOL_CONFIG warns about; and
  * `simulate_legacy` with the scale-out OFF still reproducing the original
    close-only engine trade for trade, so the rule that was added to POST
    /backtest can be shown to have changed nothing except what it was meant to.

The persistence tests below moved from a JSON file to Postgres but pin the same
guarantee -- the store can change the EDITABLE_KEYS and nothing else -- plus one
deliberate CHANGE: an unreachable store now refuses the boot instead of falling
back to the code default. None of them need a running server; the SQL itself is
covered by tests/test_db_repository.py, which skips without one.

`exit_at_mean` made that list three long and introduced the first non-float key,
so two of the tests below exist only because of it: one pins that a string is
refused rather than coerced (bool("no") is True), and one pins that a boolean
under a NUMERIC key is refused rather than floated (float(True) is 1.0, which
would have read as a legal 1.0 lots).
"""

import numpy as np
import pandas as pd
import pytest

from backend.core.errors import ConfigRejected, DatabaseUnavailable
from backend.db import repository as repo
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
    # Matches the shipped SYMBOL_CONFIG. The parity test below turns it back ON
    # explicitly, because the engine it is pinned against predates the flag.
    "exit_at_mean": False,
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
# persistence -- now Postgres, previously data/settings.json
# ---------------------------------------------------------------------------

class _FakeCursor(object):
    """Enough of a psycopg2 dict cursor to test the narrowness of the SELECT.

    A real Postgres is not needed to answer the questions these tests ask --
    "which columns does it read?" and "what does it do with a value the
    validator refuses?" -- and requiring one would mean the suite no longer
    runs offline, which CLAUDE.md pins.

    tests/test_db_repository.py covers the SQL itself against a real server,
    and skips when there is not one.
    """

    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_cursor(monkeypatch, rows):
    cur = _FakeCursor(rows)
    monkeypatch.setattr(repo, "cursor", lambda: cur)
    return cur


def test_the_store_is_only_asked_for_the_two_editable_columns(monkeypatch):
    """The narrowness is in the SELECT, not only in the caller.

    A row must never be able to introduce a symbol or move a stop. The old
    settings.json enforced that in `_load_settings`; a database needs it
    enforced at the query too, because psql, a migration and anything else
    holding the DSN can write rows the UI never could.
    """
    cur = _stub_cursor(monkeypatch, [])

    repo.load_settings(["XAUUSDm"])

    sql = cur.sql.lower()
    assert "lot_size" in sql and "partial_fraction" in sql
    assert "exit_at_mean" in sql
    # Nothing that would let the store reach a stop, a target or the P&L
    # multiplier.
    for forbidden in ("sl_pips", "tp_pips", "profit_mult", "be_trigger_pips", "pip"):
        assert forbidden not in sql
    assert "select *" not in sql
    # And only the symbols the caller named.
    assert cur.params == (["XAUUSDm"],)


def test_load_settings_ignores_unknown_symbols_and_uneditable_keys(monkeypatch):
    """Even handed a hostile payload, only the two numbers move.

    Belt and braces with the test above: that one pins the query, this one pins
    what happens if the query ever came back with more than it asked for.
    """
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "lot_size", 0.1)
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "partial_fraction", 0.5)
    monkeypatch.setattr(repo, "load_settings", lambda *a, **k: {
        "XAUUSDm": {"lot_size": 0.02, "sl_pips": 5, "profit_mult": 999},
        "EURUSDm": {"lot_size": 5.0},
    })

    bm._load_settings()

    assert bm.SYMBOL_CONFIG["XAUUSDm"]["lot_size"] == 0.02      # editable
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["partial_fraction"] == 0.5
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["sl_pips"] == 70         # not editable
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["profit_mult"] == 100    # not editable
    assert "EURUSDm" not in bm.SYMBOL_CONFIG


@pytest.mark.parametrize("stored,key", [
    ({"lot_size": -1.0, "partial_fraction": 0.5, "exit_at_mean": False}, "lot_size"),
    ({"lot_size": 0.1, "partial_fraction": 1.0, "exit_at_mean": False}, "partial_fraction"),
    ({"lot_size": 0.0, "partial_fraction": 0.5, "exit_at_mean": False}, "lot_size"),
    # `exit_at_mean` has no CHECK constraint to lean on -- BOOLEAN NOT NULL has
    # no out-of-range value -- but a dump, a JSONB round trip or a psql session
    # can still put a string in front of the validator. bool("no") is True, so
    # this is the case where a silent coercion turns the rule ON while the
    # dashboard reports it off.
    ({"lot_size": 0.1, "partial_fraction": 0.5, "exit_at_mean": "no"}, "exit_at_mean"),
])
def test_out_of_range_stored_values_are_dropped_not_returned(monkeypatch, stored, key):
    """A value the validator refuses does not reach SYMBOL_CONFIG.

    The CHECK constraints in schema.sql refuse these on the way IN, which the
    file store could not do -- but a database that predates a constraint, or one
    restored from an older dump, can still hold one. So the read validates too.
    """
    row = {"symbol": "XAUUSDm"}
    row.update(stored)
    _stub_cursor(monkeypatch, [row])
    rejected = []

    loaded = repo.load_settings(["XAUUSDm"], validate=bm._validated,
                                on_reject=lambda sym, k, exc: rejected.append(k))

    assert key not in loaded.get("XAUUSDm", {})
    assert rejected == [key]


def test_a_bad_row_leaves_the_code_default_standing(monkeypatch):
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "lot_size", 0.1)
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "partial_fraction", 0.5)
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "exit_at_mean", False)
    _stub_cursor(monkeypatch, [
        {"symbol": "XAUUSDm", "lot_size": -1.0, "partial_fraction": 1.0,
         "exit_at_mean": "yes"},
    ])

    bm._load_settings()

    assert bm.SYMBOL_CONFIG["XAUUSDm"]["lot_size"] == 0.1
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["partial_fraction"] == 0.5
    # A rejected flag leaves the code default standing too -- it does NOT fall
    # through to a truthy coercion of the string.
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["exit_at_mean"] is False


def test_an_unreachable_store_refuses_the_boot_rather_than_trading_the_default(monkeypatch):
    """The behaviour change from the file store, pinned deliberately.

    A corrupt settings.json fell back to the code defaults so the API could
    still boot. That is the wrong trade now and it was arguably always the wrong
    trade: `lot_size` is the only risk control this bot has, so falling back
    means quietly restoring ~$70/trade for someone who had lowered it. An
    unreachable store must stop the boot, not be papered over.
    """
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "lot_size", 0.02)

    def boom():
        raise DatabaseUnavailable("connection refused")

    monkeypatch.setattr(repo, "schema_version", boom)

    with pytest.raises(DatabaseUnavailable):
        bm.init_persistence()

    # And it did not helpfully "reset" anything on the way out.
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["lot_size"] == 0.02


def test_a_settings_edit_that_the_store_refuses_is_not_applied_in_memory(monkeypatch):
    """A size the database would not take must not be traded anyway.

    Writing SYMBOL_CONFIG first and persisting second would leave the process
    trading a value that vanishes on restart -- the silent-restore failure from
    the other direction, and harder to notice because the dashboard would show
    the number it accepted.
    """
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "lot_size", 0.1)
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "partial_fraction", 0.5)
    monkeypatch.setattr(bm, "bot_positions", lambda symbol: [])
    monkeypatch.setattr(bm, "_volume_limits", lambda symbol: (0.01, 100.0, 0.01, False))

    def boom(*a, **k):
        raise DatabaseUnavailable("connection refused")

    monkeypatch.setattr(repo, "save_settings", boom)
    manager = bm.BotManager.__new__(bm.BotManager)   # no MT5 initialize()

    with pytest.raises(DatabaseUnavailable):
        manager.update_settings("XAUUSDm", lot_size=0.5, scale_out_lots=0.25)

    assert bm.SYMBOL_CONFIG["XAUUSDm"]["lot_size"] == 0.1
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["partial_fraction"] == 0.5


def test_a_settings_edit_stores_what_it_applies(monkeypatch):
    """The value written to SYMBOL_CONFIG is the value the store returned.

    Not the value that was typed: the store is the authority, and reading the
    write back is what keeps the two from diverging.
    """
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "lot_size", 0.1)
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "partial_fraction", 0.5)
    monkeypatch.setattr(bm, "bot_positions", lambda symbol: [])
    monkeypatch.setattr(bm, "_volume_limits", lambda symbol: (0.01, 100.0, 0.01, False))
    written = {}

    def fake_save(symbol, lot, fraction, at_mean, source=None, notes=None):
        written["args"] = (symbol, lot, fraction, at_mean, source)
        return {"lot_size": lot, "partial_fraction": fraction,
                "exit_at_mean": at_mean}

    monkeypatch.setattr(repo, "save_settings", fake_save)
    manager = bm.BotManager.__new__(bm.BotManager)

    result = manager.update_settings("XAUUSDm", lot_size=0.2, scale_out_lots=0.05)

    # exit_at_mean was not submitted, so the value already in SYMBOL_CONFIG is
    # resolved and written through rather than defaulted -- a partial write would
    # leave the audit row claiming a change nobody asked for.
    assert written["args"] == ("XAUUSDm", 0.2, 0.25, False, "api")
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["lot_size"] == 0.2
    # 0.05 of 0.2 is a QUARTER, and a quarter is what is stored -- not the 0.05.
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["partial_fraction"] == 0.25
    assert result["scale_out_lots"] == 0.05


def test_an_edit_is_still_refused_while_a_position_is_open(monkeypatch):
    """Unchanged by the move to Postgres, and checked here because it is the one
    rule that stops manage_position() scaling a trade out twice."""
    monkeypatch.setattr(bm, "bot_positions", lambda symbol: [object()])
    monkeypatch.setattr(bm, "_volume_limits", lambda symbol: (0.01, 100.0, 0.01, False))
    monkeypatch.setattr(repo, "save_settings",
                        lambda *a, **k: pytest.fail("must not write while a position is open"))
    manager = bm.BotManager.__new__(bm.BotManager)

    with pytest.raises(ConfigRejected):
        manager.update_settings("XAUUSDm", lot_size=0.5, scale_out_lots=0.25)


# ---------------------------------------------------------------------------
# exit_at_mean -- the third editable key
# ---------------------------------------------------------------------------

def test_a_sizing_edit_does_not_reset_the_exit_rule(monkeypatch):
    """The silent-reset hazard, and the reason save_settings has no default for it.

    One statement writes all three columns, so a caller that omitted the flag
    would turn the rule off for someone who had turned it on -- and the audit row
    would record that as a deliberate choice, because the audit row is written
    from the same statement. `update_settings` therefore resolves the current
    value and writes it through.
    """
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "lot_size", 0.1)
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "partial_fraction", 0.5)
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "exit_at_mean", True)
    monkeypatch.setattr(bm, "bot_positions", lambda symbol: [])
    monkeypatch.setattr(bm, "_volume_limits", lambda symbol: (0.01, 100.0, 0.01, False))
    seen = {}

    def fake_save(symbol, lot, fraction, at_mean, source=None, notes=None):
        seen["at_mean"] = at_mean
        return {"lot_size": lot, "partial_fraction": fraction,
                "exit_at_mean": at_mean}

    monkeypatch.setattr(repo, "save_settings", fake_save)
    manager = bm.BotManager.__new__(bm.BotManager)

    manager.update_settings("XAUUSDm", lot_size=0.2, scale_out_lots=0.05)

    assert seen["at_mean"] is True
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["exit_at_mean"] is True


def test_the_exit_rule_can_be_changed_while_a_position_is_open(monkeypatch):
    """The carve-out from the sizing lock, and why it is not politeness.

    The lock exists because manage_position() infers "has the scale-out fired?"
    from the position's volume against lot_size. `exit_at_mean` takes part in no
    such inference -- and the moment someone reaches for this switch is while a
    trade is running and the centre line is closing in on it, so refusing it
    then would withhold the control in the only situation that motivates it.
    """
    monkeypatch.setitem(bm.SYMBOL_CONFIG["XAUUSDm"], "exit_at_mean", True)
    monkeypatch.setattr(bm, "bot_positions", lambda symbol: [object()])
    monkeypatch.setattr(bm, "_volume_limits", lambda symbol: (0.01, 100.0, 0.01, False))
    seen = {}

    def fake_save(symbol, lot, fraction, at_mean, source=None, notes=None):
        seen["at_mean"] = at_mean
        return {"lot_size": lot, "partial_fraction": fraction,
                "exit_at_mean": at_mean}

    monkeypatch.setattr(repo, "save_settings", fake_save)
    manager = bm.BotManager.__new__(bm.BotManager)

    manager.update_settings("XAUUSDm", exit_at_mean=False)

    assert seen["at_mean"] is False
    assert bm.SYMBOL_CONFIG["XAUUSDm"]["exit_at_mean"] is False
    # ...while the sizing fields are still refused on the same call shape.
    with pytest.raises(ConfigRejected):
        manager.update_settings("XAUUSDm", lot_size=0.5, scale_out_lots=0.25)


def test_validated_takes_a_bool_for_the_flag_and_nothing_else():
    assert bm._validated("exit_at_mean", True) is True
    assert bm._validated("exit_at_mean", False) is False
    # 0/1 survive because a JSONB round trip or an older dump can produce them.
    assert bm._validated("exit_at_mean", 1) is True
    assert bm._validated("exit_at_mean", 0) is False
    for bad in ("true", "false", "", None, 2, 0.5):
        with pytest.raises(ConfigRejected):
            bm._validated("exit_at_mean", bad)


def test_validated_refuses_a_boolean_under_a_numeric_key():
    """bool is a subclass of int, so float(True) is 1.0.

    A boolean landing under `lot_size` -- a mis-keyed row, a JSON payload with
    the fields transposed -- would validate cleanly as 1.0 lots, ten times the
    shipped size and ~$700 a trade on gold, with every range check passing it.
    This function only started seeing booleans when exit_at_mean was added, so
    this hole is new and the guard is not theoretical.
    """
    for key in ("lot_size", "partial_fraction"):
        for bad in (True, False):
            with pytest.raises(ConfigRejected):
                bm._validated(key, bad)


def test_the_live_centre_line_exit_is_a_no_op_when_the_flag_is_off(monkeypatch):
    """The rule that closed the reported trade. It had no test before this.

    A live XAUUSDm short entered at 4485.183 (SL 4492.183, TP 4475.183) banked
    half at 4480.183 and pulled its stop to break-even, then closed HERE at
    4479.196 -- short of the 10.00 target it had been left running for. The
    block has no scale-out awareness at all, so with the flag on it races the
    break-even stop and the target on every trade that banks a partial.
    """
    bot = bm.TradingBot.__new__(bm.TradingBot)
    bot.symbol = "XAUUSDm"
    bot.config = dict(BASE, exit_at_mean=False)
    closed = []
    bot.close_position = lambda pos, comment=None: closed.append((pos, comment))

    short = type("P", (), {"type": mt5.POSITION_TYPE_SELL, "ticket": 1})()
    # 4479.196 is below the centre line, so the rule WOULD fire if it were on.
    bot._mean_reversion_exit([short], 4479.196, 4480.0)
    assert closed == []

    bot.config = dict(BASE, exit_at_mean=True)
    bot._mean_reversion_exit([short], 4479.196, 4480.0)
    # Its own comment, so a centre-line close can be told apart in
    # deals.comment. It used to send close_position's default, which is why the
    # live trade could not be attributed from the ledger at all.
    assert closed == [(short, "NW mean reversion")]


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
    """With BOTH later rules set to their pre-existing state, nothing moved.

    `exit_at_mean=True` is not incidental: `_original_engine` below is a verbatim
    copy of the loop as it stood before either rule was configurable, and that
    loop closed on a return to the centre line unconditionally. Passing the flag
    here -- rather than editing the reference -- keeps the artifact honest and
    states the invariant precisely: scale-out off plus centre-line exit on is
    byte-for-byte the original engine.
    """
    df = make_ohlc(kind, n=1600, seed=5)
    outs, uppers, lowers = _envelope(df)
    config = cfg(partial_fraction=0.0, exit_at_mean=True)

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


def test_the_flag_off_lets_a_scaled_out_runner_reach_its_target():
    """The reported incident, priced, on the engine behind the Backtest page.

    One SELL entered at 4485.183 with the shipped gold geometry: stop 4492.183,
    target 4475.183, scale-out at +5.00 = 4480.183.

      * flag ON  -- banks 5.00 x 0.05 x 100 = $25.00 at the trigger, then the
        runner is closed at the centre line on the bar that prints 4479.196:
        5.987 x 0.05 x 100 = $29.94. Total $54.94.
      * flag OFF -- the centre-line bar does nothing and the next bar reaches
        the target: 10.00 x 0.05 x 100 = $50.00. Total $75.00.

    $20.06 on one trade, and the scale-out fires either way -- the flag cannot
    reach the trigger, only what happens to the half left running.
    """
    close = [4485.183, 4480.0, 4479.196, 4475.0]
    df = pd.DataFrame({"close": close})
    n = len(close)
    uppers = np.array([4480.0, 4600.0, 4600.0, 4600.0])   # bar 0 enters short
    lowers = np.full(n, 4000.0)
    outs = np.array([4470.0, 4470.0, 4479.196, 4470.0])   # bar 2 reaches the mean

    on = bm.simulate_legacy(df, outs, uppers, lowers,
                            cfg(partial_fraction=0.5, exit_at_mean=True), 1000.0)
    off = bm.simulate_legacy(df, outs, uppers, lowers,
                             cfg(partial_fraction=0.5, exit_at_mean=False), 1000.0)

    assert on["partials_fired"] == 1 and off["partials_fired"] == 1
    assert on["total_pl"] == pytest.approx(54.935, abs=1e-3)
    assert off["total_pl"] == pytest.approx(75.0, abs=1e-3)


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
