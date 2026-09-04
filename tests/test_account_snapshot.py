"""The account panel has to say when its numbers stopped moving.

`get_account_info()` serves `account_snapshots` through a 60-second throttle,
because the `time_profits` behind a fresh reading are four 365-day
`history_deals_get()` calls over IPC and the dashboard polls every 5 seconds.
That throttle has a second exit nobody was reading: if the row is older than
the limit but MT5 does not answer, `_capture_account_snapshot()` returns None
and the stored row is served anyway. That fallback is the right call -- the last
known balance beats an empty panel -- but it used to hardcode `"stale": False`,
so a reading taken at 11:04 was served at 17:53 as a live one, under a note
promising it refreshes once a minute.

Nothing here needs Postgres or a terminal: `repo` and the MT5 read are both
stubbed, which is the point -- the branch only happens when one of them is
down, so it cannot be covered by a test that requires them up.
"""

import pytest

pytest.importorskip("MetaTrader5", reason="bot_manager imports MetaTrader5")

from backend import bot_manager as bm


ROW = {
    "balance": 7452.11,
    "equity": 7452.11,
    "profit": 0.0,
    "leverage": 50,
    "margin": 0.0,
    "drawdown_pct": 0.0,
    "period_profits": {"daily": 355.64},
    "captured_at": "2026-09-04T11:04:07+00:00",
}


def manager(monkeypatch, age, capture=None):
    """A BotManager with the two things this branch depends on stubbed out.

    `__new__` rather than `__init__` because the constructor starts a database
    and an MT5 connection, and neither exists in the situation under test.
    """
    mgr = bm.BotManager.__new__(bm.BotManager)
    monkeypatch.setattr(bm.repo, "account_snapshot_age_seconds", lambda: age)
    monkeypatch.setattr(bm.repo, "latest_account_snapshot", lambda: dict(ROW))
    monkeypatch.setattr(
        bm.BotManager, "_capture_account_snapshot", lambda self, persist=True: capture)
    return mgr


# ---------------------------------------------------------------------------

def test_a_row_inside_the_throttle_is_not_stale(monkeypatch):
    """The ordinary path: the reading is 12 seconds old, so it is fresh and the
    four history scans are correctly skipped."""
    info = manager(monkeypatch, age=12.0).get_account_info()

    assert info["stale"] is False
    assert info["age_seconds"] == 12.0
    assert info["balance"] == 7452.11


def test_a_row_served_because_mt5_is_down_is_stale(monkeypatch):
    """The bug. Past the throttle with no capture, so the row is ~7h old."""
    info = manager(monkeypatch, age=24540.0).get_account_info()

    assert info["stale"] is True, "a 6.8-hour-old reading was reported as live"
    assert info["age_seconds"] == 24540.0
    # Still served, and still stamped -- degrading to the last known balance is
    # the intended behaviour; claiming it is current was not.
    assert info["balance"] == 7452.11
    assert info["captured_at"] == ROW["captured_at"]


def test_a_freshly_captured_reading_is_not_stale(monkeypatch):
    """When MT5 does answer, the capture's own dict is returned -- and it has to
    carry the same keys, or the panel sees `age_seconds` appear and disappear."""
    captured = {
        "balance": 1.0, "equity": 1.0, "profit": 0.0, "leverage": 50,
        "margin": 0.0, "drawdown": 0.0, "time_profits": {},
        "captured_at": "2026-09-04T17:53:31+00:00", "age_seconds": 0.0,
        "stale": False,
    }
    info = manager(monkeypatch, age=24540.0, capture=captured).get_account_info()

    assert info["stale"] is False
    assert info["balance"] == 1.0
    assert set(info) == set(manager(monkeypatch, age=12.0).get_account_info())


def test_the_stale_flag_is_derived_and_not_hardcoded(monkeypatch):
    """Pins the flag against the throttle it is measured with, so raising
    BOT_ACCOUNT_SNAPSHOT_SECONDS cannot leave the two disagreeing."""
    limit = bm.BotManager.ACCOUNT_SNAPSHOT_MAX_AGE

    assert manager(monkeypatch, age=limit - 0.5).get_account_info()["stale"] is False
    assert manager(monkeypatch, age=limit).get_account_info()["stale"] is True


def test_no_stored_row_at_all_returns_an_empty_panel(monkeypatch):
    """`age is None` means the table is empty. Nothing to label, so the caller
    gets {} and the panel renders its empty state rather than zeros, which read
    as a real account that has lost all its money."""
    mgr = bm.BotManager.__new__(bm.BotManager)
    monkeypatch.setattr(bm.repo, "account_snapshot_age_seconds", lambda: None)
    monkeypatch.setattr(bm.repo, "latest_account_snapshot", lambda: None)
    monkeypatch.setattr(
        bm.BotManager, "_capture_account_snapshot", lambda self, persist=True: None)

    assert mgr.get_account_info() == {}
