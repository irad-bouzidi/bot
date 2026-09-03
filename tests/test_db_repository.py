"""The SQL, against a real Postgres. SKIPPED when there is not one.

`python -m pytest` must keep passing with no server running (CLAUDE.md), so
every test here is behind a module-level skip. To run them:

    docker compose up -d db
    python -m backend.db.migrate
    python -m pytest tests/test_db_repository.py

They run against a SCRATCH SCHEMA (`test_repo_<pid>`) inside the same database,
created and dropped per session. Not the live schema: `symbol_settings` holds
the lot size the bot actually trades, and a test that truncated it would hand
the next start-up the code default -- the silent restore the whole persistence
path exists to prevent.

The trade fold gets the most attention because it is the only substantial piece
of logic that lives in SQL rather than in Python, and because getting it wrong
is quiet: the numbers stay plausible.
"""

import os
import uuid
from datetime import datetime, timedelta

import pytest

from backend.core.errors import DatabaseUnavailable
from backend.db import pool, repository as repo

if not pool.ping():
    pytest.skip(
        "No Postgres at %s. Start it with `docker compose up -d db`."
        % pool.redact(pool.database_url()),
        allow_module_level=True,
    )

SCHEMA = "test_repo_%d" % os.getpid()
MAGIC = 123456


@pytest.fixture(scope="module", autouse=True)
def scratch_schema():
    """A throwaway schema, so nothing here can touch the live sizing."""
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
        cur.execute("CREATE SCHEMA %s" % SCHEMA)
        cur.close()

    # search_path is per-connection and the pool hands out several, so it is set
    # as a database-level default rather than once on one connection. Every
    # connection the pool opens from here on lands in the scratch schema.
    dbname = _current_database()
    _set_search_path(dbname, SCHEMA)
    pool.close_pool()
    try:
        repo.apply_schema()
        yield
    finally:
        _set_search_path(dbname, "public")
        pool.close_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
            cur.close()


def _current_database():
    with repo.cursor() as cur:
        cur.execute("SELECT current_database() AS db")
        return cur.fetchone()["db"]


def _set_search_path(dbname, schema):
    with pool.connection() as conn:
        # ALTER DATABASE ... SET cannot run inside a transaction block.
        conn.set_session(autocommit=True)
        cur = conn.cursor()
        cur.execute('ALTER DATABASE "%s" SET search_path TO %s' % (dbname, schema))
        cur.close()
        conn.set_session(autocommit=False)


@pytest.fixture(autouse=True)
def clean_tables():
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute("TRUNCATE deals, trades, symbol_settings, settings_audit, "
                    "bot_state, bot_snapshots, control_events, backtest_runs, "
                    "account_snapshots, ui_preferences")
        cur.close()
    yield


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_TICKET = [1]


def deal(position_id, kind, deal_type, volume, price, profit=0.0,
         commission=0.0, swap=0.0, fee=0.0, at=None, symbol="XAUUSDm",
         magic=MAGIC, comment=None):
    _TICKET[0] += 1
    return {
        "ticket": _TICKET[0],
        "order_ticket": _TICKET[0],
        "position_id": position_id,
        "symbol": symbol,
        "magic": magic,
        "entry_kind": kind,
        "deal_type": deal_type,
        "volume": volume,
        "price": price,
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "fee": fee,
        "comment": comment,
        "dealt_at": at or datetime(2026, 1, 1, 12, 0, 0),
    }


def one_trade(symbol="XAUUSDm"):
    rows = repo.list_trades(symbol)
    assert len(rows) == 1, rows
    return rows[0]


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------

def test_settings_round_trip():
    repo.save_settings("XAUUSDm", 0.03, 0.25)
    assert repo.load_settings(["XAUUSDm"]) == {
        "XAUUSDm": {"lot_size": 0.03, "partial_fraction": 0.25},
    }


def test_settings_are_only_returned_for_the_symbols_asked_for():
    repo.save_settings("XAUUSDm", 0.03, 0.25)
    repo.save_settings("EURUSDm", 0.5, 0.0)
    loaded = repo.load_settings(["XAUUSDm"])
    assert list(loaded) == ["XAUUSDm"]


@pytest.mark.parametrize("lot,fraction", [(0.0, 0.5), (-1.0, 0.5), (0.1, 1.0), (0.1, -0.1)])
def test_the_column_constraints_refuse_an_out_of_range_value(lot, fraction):
    """The CHECK constraints, which the JSON file had no equivalent of.

    _validated() is the first line of defence and runs in the API. This is the
    second, and it is the one that also covers psql, a migration, and a restore
    from an older dump.
    """
    with pytest.raises(Exception) as exc:
        repo.save_settings("XAUUSDm", lot, fraction)
    assert "symbol_settings" in str(exc.value)


def test_each_save_records_what_it_replaced():
    repo.save_settings("XAUUSDm", 0.1, 0.5)
    repo.save_settings("XAUUSDm", 0.02, 0.25, notes=["lowered on purpose"])

    history = repo.settings_history("XAUUSDm")
    assert len(history) == 2
    newest = history[0]
    assert (newest["prev_lot_size"], newest["lot_size"]) == (0.1, 0.02)
    assert newest["notes"] == "lowered on purpose"
    # The first save had nothing to replace.
    assert history[1]["prev_lot_size"] is None


# ---------------------------------------------------------------------------
# the trade fold
# ---------------------------------------------------------------------------

def test_a_simple_winning_trade_folds_to_one_closed_row():
    repo.upsert_deals([
        deal(1, "in", "buy", 0.1, 3300.0, at=datetime(2026, 1, 1, 10, 0)),
        deal(1, "out", "sell", 0.1, 3310.0, profit=100.0, commission=-0.7,
             at=datetime(2026, 1, 1, 11, 0)),
    ])
    repo.rebuild_trades("XAUUSDm")

    t = one_trade()
    assert t["side"] == "long"
    assert t["status"] == "closed"
    assert t["entry_price"] == 3300.0
    assert t["exit_price"] == 3310.0
    assert t["exit_count"] == 1
    assert t["gross_profit"] == 100.0
    # Costs are SUMMED, because MT5 reports them already signed negative.
    # Subtracting would turn a $0.70 charge into a $0.70 credit.
    assert t["net_profit"] == pytest.approx(99.3)
    assert t["closed_at"] is not None


def test_side_comes_from_the_entry_deal_not_the_exit():
    """A long is closed by a SELL deal. Reading side off any exit inverts every
    trade in the table, and the P&L stays right while it does."""
    repo.upsert_deals([
        deal(2, "in", "sell", 0.1, 3300.0, at=datetime(2026, 1, 1, 10, 0)),
        deal(2, "out", "buy", 0.1, 3290.0, profit=100.0, at=datetime(2026, 1, 1, 11, 0)),
    ])
    repo.rebuild_trades("XAUUSDm")
    assert one_trade()["side"] == "short"


def test_a_scaled_out_trade_is_one_row_not_two():
    """The defect this whole table exists to fix.

    update_performance_stats() counted every closing deal separately, so this
    sequence -- bank half at +5, then get stopped at break-even -- booked ONE
    WIN and one flat. Two outcomes for one trade, the win rate lifted by the
    scale-out, and no loss recorded against a trade that ended down on costs.
    """
    repo.upsert_deals([
        deal(3, "in", "buy", 0.1, 3300.0, commission=-0.7,
             at=datetime(2026, 1, 1, 10, 0)),
        deal(3, "out", "sell", 0.05, 3305.0, profit=25.0, commission=-0.35,
             at=datetime(2026, 1, 1, 10, 30), comment="NW partial TP"),
        deal(3, "out", "sell", 0.05, 3300.0, profit=0.0, commission=-0.35,
             at=datetime(2026, 1, 1, 11, 0)),
    ])
    repo.rebuild_trades("XAUUSDm")

    t = one_trade()
    assert t["exit_count"] == 2          # this IS the scale-out having fired
    assert t["volume_in"] == pytest.approx(0.1)
    assert t["volume_out"] == pytest.approx(0.1)
    assert t["status"] == "closed"
    assert t["gross_profit"] == pytest.approx(25.0)
    assert t["net_profit"] == pytest.approx(25.0 - 1.4)
    # Volume-weighted across both exits, not whichever was last.
    assert t["exit_price"] == pytest.approx(3302.5)

    stats = repo.trade_stats("XAUUSDm")
    assert (stats["trades_closed"], stats["wins"], stats["losses"]) == (1, 1, 0)
    assert stats["scaled_out"] == 1


def test_a_partially_closed_position_is_still_open_and_undated():
    """A scale-out is not a close.

    Dating the trade by its partial exit would drop it into the closed-trade
    equity curve while the runner is still live, and count a result that has
    not happened yet.
    """
    repo.upsert_deals([
        deal(4, "in", "buy", 0.1, 3300.0, at=datetime(2026, 1, 1, 10, 0)),
        deal(4, "out", "sell", 0.05, 3305.0, profit=25.0,
             at=datetime(2026, 1, 1, 10, 30)),
    ])
    repo.rebuild_trades("XAUUSDm")

    t = one_trade()
    assert t["status"] == "open"
    assert t["closed_at"] is None
    stats = repo.trade_stats("XAUUSDm")
    assert stats["trades_open"] == 1
    assert stats["trades_closed"] == 0
    # And its unrealised profit is NOT in the total.
    assert stats["total_pl"] == 0.0


def test_re_reading_the_same_deals_does_not_double_count():
    """The property the whole reconcile loop rests on.

    The window overlaps deliberately -- `swap` lands on a deal after the fact --
    so the upsert has to correct a row it has already seen rather than add one.
    """
    rows = [
        deal(5, "in", "buy", 0.1, 3300.0, at=datetime(2026, 1, 1, 10, 0)),
        deal(5, "out", "sell", 0.1, 3310.0, profit=100.0,
             at=datetime(2026, 1, 1, 11, 0)),
    ]
    for _ in range(3):
        repo.upsert_deals(rows)
        repo.rebuild_trades("XAUUSDm")

    assert repo.count_trades("XAUUSDm") == 1
    assert one_trade()["net_profit"] == pytest.approx(100.0)

    # And a later swap credit CORRECTS the stored row.
    rows[1]["swap"] = -2.5
    repo.upsert_deals(rows)
    repo.rebuild_trades("XAUUSDm")
    assert one_trade()["net_profit"] == pytest.approx(97.5)


def test_a_losing_trade_is_counted_as_a_loss():
    """"Never hide losing trades" (Trading Bot.md), as an assertion."""
    repo.upsert_deals([
        deal(6, "in", "buy", 0.1, 3300.0, at=datetime(2026, 1, 1, 10, 0)),
        deal(6, "out", "sell", 0.1, 3293.0, profit=-70.0,
             at=datetime(2026, 1, 1, 11, 0)),
    ])
    repo.rebuild_trades("XAUUSDm")

    stats = repo.trade_stats("XAUUSDm")
    assert (stats["wins"], stats["losses"]) == (0, 1)
    assert stats["total_pl"] == pytest.approx(-70.0)
    assert stats["win_rate"] == 0.0


def test_a_trade_that_only_costs_is_a_loss_not_a_win():
    """Decided on NET, so a gross-flat trade that paid commission is a loss."""
    repo.upsert_deals([
        deal(7, "in", "buy", 0.1, 3300.0, commission=-0.7,
             at=datetime(2026, 1, 1, 10, 0)),
        deal(7, "out", "sell", 0.1, 3300.0, profit=0.0, commission=-0.7,
             at=datetime(2026, 1, 1, 11, 0)),
    ])
    repo.rebuild_trades("XAUUSDm")

    stats = repo.trade_stats("XAUUSDm")
    assert stats["losses"] == 1
    assert stats["breakeven"] == 0


def test_break_even_trades_are_reported_beside_the_rate_not_folded_into_it():
    """A dead-flat trade is the designed outcome of the break-even stop.

    Counting it as a loss understates the rule and counting it as a win
    overstates it, so it is excluded from the denominator and reported
    separately -- with the count returned so the choice is visible.
    """
    base = datetime(2026, 1, 1, 10, 0)
    repo.upsert_deals([
        deal(8, "in", "buy", 0.1, 3300.0, at=base),
        deal(8, "out", "sell", 0.1, 3310.0, profit=100.0, at=base + timedelta(hours=1)),
        deal(9, "in", "buy", 0.1, 3300.0, at=base),
        deal(9, "out", "sell", 0.1, 3300.0, profit=0.0, at=base + timedelta(hours=2)),
    ])
    repo.rebuild_trades("XAUUSDm")

    stats = repo.trade_stats("XAUUSDm")
    assert (stats["wins"], stats["losses"], stats["breakeven"]) == (1, 0, 1)
    assert stats["win_rate"] == 100.0     # 1 of 1 DECIDED trade
    assert stats["trades_closed"] == 2


def test_max_drawdown_is_the_deepest_peak_to_trough_of_the_closed_curve():
    """+100, -70, -70, +100: peak 100, trough -40, so 140.

    The window that computes the running peak has to advance in the order the
    curve accumulates in. Ordered by value instead, the peak would always be
    the current row and this would return 0 on every input -- which looks
    exactly like the old stat that was never written to at all.
    """
    base = datetime(2026, 1, 1, 10, 0)
    rows = []
    for i, profit in enumerate([100.0, -70.0, -70.0, 100.0]):
        pid = 100 + i
        at = base + timedelta(hours=i)
        rows.append(deal(pid, "in", "buy", 0.1, 3300.0, at=at))
        rows.append(deal(pid, "out", "sell", 0.1, 3300.0, profit=profit,
                         at=at + timedelta(minutes=30)))
    repo.upsert_deals(rows)
    repo.rebuild_trades("XAUUSDm")

    stats = repo.trade_stats("XAUUSDm")
    assert stats["total_pl"] == pytest.approx(60.0)
    assert stats["max_drawdown"] == pytest.approx(140.0)


def test_a_monotonically_rising_curve_has_no_drawdown():
    base = datetime(2026, 1, 1, 10, 0)
    rows = []
    for i in range(3):
        pid = 200 + i
        at = base + timedelta(hours=i)
        rows.append(deal(pid, "in", "buy", 0.1, 3300.0, at=at))
        rows.append(deal(pid, "out", "sell", 0.1, 3300.0, profit=10.0,
                         at=at + timedelta(minutes=30)))
    repo.upsert_deals(rows)
    repo.rebuild_trades("XAUUSDm")
    assert repo.trade_stats("XAUUSDm")["max_drawdown"] == pytest.approx(0.0)


def test_the_fold_is_scoped_to_one_symbol():
    repo.upsert_deals([
        deal(300, "in", "buy", 0.1, 3300.0),
        deal(300, "out", "sell", 0.1, 3310.0, profit=100.0),
        deal(301, "in", "buy", 0.1, 1.1, symbol="EURUSDm"),
        deal(301, "out", "sell", 0.1, 1.2, profit=50.0, symbol="EURUSDm"),
    ])
    repo.rebuild_trades("XAUUSDm")

    assert repo.count_trades("XAUUSDm") == 1
    assert repo.count_trades("EURUSDm") == 0


def test_deals_with_no_entry_are_not_folded_into_a_trade():
    """A window that starts mid-trade sees the exit and not the entry.

    Such a group has no side and no entry price, so it is skipped rather than
    written as a trade with nulls where the important numbers go. The next
    full reconcile picks it up whole.
    """
    repo.upsert_deals([
        deal(400, "out", "sell", 0.1, 3310.0, profit=100.0),
    ])
    repo.rebuild_trades("XAUUSDm")
    assert repo.count_trades("XAUUSDm") == 0


def test_the_deals_behind_a_trade_are_retrievable():
    repo.upsert_deals([
        deal(500, "in", "buy", 0.1, 3300.0, at=datetime(2026, 1, 1, 10, 0)),
        deal(500, "out", "sell", 0.05, 3305.0, profit=25.0,
             at=datetime(2026, 1, 1, 10, 30)),
        deal(500, "out", "sell", 0.05, 3300.0, at=datetime(2026, 1, 1, 11, 0)),
    ])
    deals = repo.list_deals(500)
    assert [d["entry_kind"] for d in deals] == ["in", "out", "out"]


# ---------------------------------------------------------------------------
# bot state
# ---------------------------------------------------------------------------

def test_bar_marks_survive_a_restart():
    """The S4 guards, which were instance attributes and reset on every thread.

    Cleared, the bot can enter again on the very bar it just entered on -- the
    repeat-fire S4 exists to stop, reachable by pressing Stop then Start.
    """
    repo.ensure_bot_rows(["XAUUSDm"])
    repo.set_bar_marks("XAUUSDm", last_bar_time=1700000000, last_entry_bar=1699999700)
    assert repo.get_bar_marks("XAUUSDm") == (1700000000, 1699999700)


def test_setting_one_bar_mark_does_not_clear_the_other():
    repo.ensure_bot_rows(["XAUUSDm"])
    repo.set_bar_marks("XAUUSDm", last_bar_time=1700000000, last_entry_bar=1699999700)
    repo.set_bar_marks("XAUUSDm", last_bar_time=1700000300)
    assert repo.get_bar_marks("XAUUSDm") == (1700000300, 1699999700)


def test_desired_state_is_recorded_separately_from_the_thread():
    repo.set_desired_state("XAUUSDm", "running")
    row = repo.get_bot_state("XAUUSDm")
    assert row["desired_state"] == "running"
    assert row["started_at"] is not None

    repo.set_desired_state("XAUUSDm", "stopped")
    row = repo.get_bot_state("XAUUSDm")
    assert row["desired_state"] == "stopped"
    # The start time is kept, not overwritten -- it is when it last ran.
    assert row["started_at"] is not None
    assert row["stopped_at"] is not None


def test_refused_control_presses_are_recorded_too():
    repo.record_control_event("XAUUSDm", "start", True, None)
    repo.record_control_event("NOPE", "start", False, "unsupported symbol")
    events = repo.list_control_events()
    assert [e["accepted"] for e in events] == [False, True]   # newest first


def test_a_stopped_bot_keeps_its_last_envelope_reading():
    """So a stopped card shows where the bands were, not 0.00."""
    repo.save_snapshot("XAUUSDm", "Running", last_close=3300.0, nw_out=3299.0,
                       nw_upper=3310.0, nw_lower=3288.0, bar_time=1700000000,
                       open_positions=1)
    repo.set_snapshot_status("XAUUSDm", "Stopped")

    snap = repo.get_snapshots()["XAUUSDm"]
    assert snap["status"] == "Stopped"
    assert snap["nw_upper"] == 3310.0


# ---------------------------------------------------------------------------
# backtest runs
# ---------------------------------------------------------------------------

def test_a_backtest_run_stores_its_inputs_with_its_result():
    stored = repo.record_backtest(
        "XAUUSDm", datetime(2025, 5, 1), datetime(2025, 6, 1), 1000.0,
        lot_size=0.1, scale_out_lots=0.05, partial_fraction=0.5,
        result={"total_pl": -12.5, "win_rate": 45.9}, duration_ms=1234)

    run = repo.get_backtest(stored["id"])
    assert run["status"] == "ok"
    assert run["result"]["total_pl"] == -12.5
    assert run["lot_size"] == 0.1
    assert run["engine"] == "legacy"


def test_a_failed_backtest_run_is_stored_too():
    """A window with no bars is a fact about that window. Dropping it is how the
    same unavailable range gets asked for five times."""
    repo.record_backtest("XAUUSDm", datetime(2020, 1, 1), datetime(2020, 2, 1),
                         1000.0, status="error",
                         error="No historical data found for the given range")
    runs = repo.list_backtests("XAUUSDm")
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert runs[0]["result"] is None


# ---------------------------------------------------------------------------
# preferences
# ---------------------------------------------------------------------------

def test_preferences_are_merged_not_replaced():
    """The theme switch and the backtest form both write here. A replace would
    mean whichever fired last erased the other's fields."""
    repo.save_preferences({"theme": "dark"})
    repo.save_preferences({"view": "trades"})

    prefs = repo.get_preferences()
    assert prefs == {"theme": "dark", "view": "trades"}

    repo.save_preferences({"theme": "light"})
    assert repo.get_preferences() == {"theme": "light", "view": "trades"}


def test_nested_preference_values_round_trip():
    form = {"symbol": "XAUUSDm", "lot": "0.02", "initial_balance": 1000}
    repo.save_preferences({"backtest": form})
    assert repo.get_preferences()["backtest"] == form


def test_replace_preferences_clears_the_document():
    repo.save_preferences({"theme": "dark", "view": "trades"})
    assert repo.replace_preferences({}) == {}
    assert repo.get_preferences() == {}


# ---------------------------------------------------------------------------
# account snapshots
# ---------------------------------------------------------------------------

def test_the_snapshot_age_drives_the_throttle():
    """Read from the table rather than from a module-level timestamp, so a
    restarted process does not immediately re-run the four year-long history
    scans the snapshot exists to avoid."""
    assert repo.account_snapshot_age_seconds() is None
    repo.save_account_snapshot({"balance": 1000.0, "equity": 990.0}, {"daily": -10.0})
    age = repo.account_snapshot_age_seconds()
    assert age is not None and age < 5

    latest = repo.latest_account_snapshot()
    assert latest["balance"] == 1000.0
    assert latest["period_profits"] == {"daily": -10.0}


def test_snapshots_accumulate_into_an_equity_curve():
    for equity in (1000.0, 990.0, 1010.0):
        repo.save_account_snapshot({"balance": 1000.0, "equity": equity}, {})
    curve = repo.account_equity_curve()
    assert [p["equity"] for p in curve] == [1000.0, 990.0, 1010.0]   # oldest first
