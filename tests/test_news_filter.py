"""The news blackout: window arithmetic, the two vetoes, and fail-closed.

Three groups, and the middle one is the important one:

  1. `backend/core/news.py` -- window edges, merging, filtering, parsing. Pinned
     with explicit instants rather than "now", because every one of these is an
     off-by-one or a timezone away from being wrong in a way no report would
     show.
  2. The EMPTY-CALENDAR regression. A run that says nothing about news must
     produce byte-identical signals to the code before this rule existed. That
     is what keeps every stored report in `data/reports/` comparable, and it is
     the same technique `test_sizing_settings.py` uses to prove the legacy
     engine is unchanged with the scale-out off.
  3. The live feed's verdicts, driven by an injected fetcher so the suite still
     passes with no network -- exactly as it passes with no MT5 and no Postgres.

No test here touches the network, `data/`, or a database.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from backend.backtest.engine import BacktestConfig, BacktestEngine
from backend.core import news as N
from backend.core.types import Side, SignalType
from backend.strategy.base import Bar, BarContext
from backend.strategy.nw_envelope import NWConfig, NWEnvelopeStrategy
from tests.fixtures.synthetic import make_ohlc
from tests.test_backtest_engine import SPEC, bars

UTC = timezone.utc


def at(hh, mm=0, day=4, month=9, year=2026):
    return datetime(year, month, day, hh, mm, tzinfo=UTC)


def ev(hh, mm=0, currency="USD", impact="high", title="Event", **kw):
    return N.NewsEvent(at=at(hh, mm, **kw), currency=currency, impact=impact,
                       title=title)


def cal(events, **kw):
    kw.setdefault("currencies", ["USD"])
    return N.NewsCalendar(events, **kw)


# --- 1. window arithmetic --------------------------------------------------

def test_window_is_half_open_at_both_edges():
    """Closed at T-before, OPEN at T+after.

    So a 30/30 window is exactly 60 minutes and two adjacent windows cannot both
    claim the same instant. If the upper edge were closed, T+30 would be blocked
    by one window while being the first tradeable bar of the next -- a state
    with no correct answer.
    """
    c = cal([ev(12, 30)])
    assert not c.blocked_at(at(11, 59))     # one minute before the window
    assert c.blocked_at(at(12, 0))          # exactly T-30: BLOCKED
    assert c.blocked_at(at(12, 30))         # the release itself
    assert c.blocked_at(at(12, 59))         # one minute before the far edge
    assert not c.blocked_at(at(13, 0))      # exactly T+30: TRADEABLE
    assert not c.blocked_at(at(13, 1))


def test_asymmetric_window_is_honoured():
    c = cal([ev(12, 30)], before_minutes=5, after_minutes=90)
    assert not c.blocked_at(at(12, 24))
    assert c.blocked_at(at(12, 25))
    assert c.blocked_at(at(13, 59))
    assert not c.blocked_at(at(14, 0))


def test_zero_window_blocks_only_the_instant_itself():
    """A degenerate but reachable config (BOT_NEWS_BEFORE_MIN=0)."""
    c = cal([ev(12, 30)], before_minutes=0, after_minutes=0)
    assert not c.blocked_at(at(12, 30))     # half-open: [12:30, 12:30) is empty
    assert not c.blocked_at(at(12, 29))


def test_negative_window_is_refused():
    with pytest.raises(ValueError):
        cal([ev(12, 30)], before_minutes=-1)


def test_overlapping_windows_merge_instead_of_toggling():
    """Two releases 30 minutes apart imply ONE continuous blackout.

    Unmerged, 12:59 would fall in the first window and 13:00 would fall in the
    second, but nothing would guarantee there was no gap between them -- and a
    single tradeable minute in the middle of a cluster is an entry nobody asked
    for.
    """
    c = cal([ev(12, 30), ev(13, 0)])
    assert len(c._starts) == 1, "the two windows should have merged into one"
    for hh, mm in ((12, 0), (12, 30), (12, 59), (13, 0), (13, 29)):
        assert c.blocked_at(at(hh, mm)), "%02d:%02d should be blocked" % (hh, mm)
    assert not c.blocked_at(at(13, 30))     # T+30 of the LAST event
    assert not c.blocked_at(at(11, 59))     # T-30 of the FIRST


def test_distant_events_do_not_merge_and_leave_a_real_gap():
    """The converse: 12:30 and 14:00 are 90 minutes apart, so 13:00-13:29 IS
    tradeable. Merging everything would be as wrong as merging nothing."""
    c = cal([ev(12, 30), ev(14, 0)])
    assert len(c._starts) == 2
    assert not c.blocked_at(at(13, 0))
    assert not c.blocked_at(at(13, 29))
    assert c.blocked_at(at(13, 30))


def test_a_contained_window_does_not_shorten_the_outer_one():
    """A short window fully inside a longer one must not truncate it.

    The merge walks events in time order and only ever extends the current
    interval's end, so an event whose window ends EARLIER than the running end
    must leave it alone.
    """
    c = cal([ev(12, 0), ev(12, 5)], before_minutes=60, after_minutes=60)
    assert len(c._starts) == 1
    assert c.blocked_at(at(13, 4))          # T+60 of the second event
    assert not c.blocked_at(at(13, 5))


def test_currency_filter_excludes_other_currencies():
    c = cal([ev(12, 30, currency="EUR", title="ECB")])
    assert len(c) == 0
    assert not c.blocked_at(at(12, 30))


def test_currencies_none_means_any_and_empty_means_none():
    """The two are deliberately NOT normalised together.

    `currencies=None` is "any currency"; `currencies=[]` is "no currency". A
    mis-read config that produced an empty list must block nothing loudly, not
    everything silently.
    """
    events = [ev(12, 30, currency="EUR")]
    assert N.NewsCalendar(events, currencies=None).blocked_at(at(12, 30))
    assert not N.NewsCalendar(events, currencies=[]).blocked_at(at(12, 30))


def test_impact_filter_excludes_low_by_default():
    c = cal([ev(12, 30, impact="low", title="Speech")])
    assert len(c) == 0
    c = cal([ev(12, 30, impact="medium")])
    assert c.blocked_at(at(12, 30))


def test_holiday_rows_never_black_out_at_the_default():
    """Parsed and archived, but not acted on: a bank holiday is a thin-liquidity
    day, not a release at a known instant, so a 30-minute window around whatever
    hour the feed stamped on it would be arbitrary."""
    assert "holiday" in N.IMPACTS
    assert "holiday" not in N.DEFAULT_IMPACTS
    c = cal([ev(0, 0, currency="USD", impact="holiday", title="Bank Holiday")])
    assert len(c) == 0


def test_active_event_names_the_nearest_release_in_a_cluster():
    c = cal([ev(12, 30, title="NFP"), ev(13, 0, title="ISM")])
    assert c.active_event(at(12, 35)).title == "NFP"
    assert c.active_event(at(12, 55)).title == "ISM"
    assert c.active_event(at(9, 0)) is None


def test_next_event_after_is_strictly_after():
    c = cal([ev(12, 30, title="NFP"), ev(14, 0, title="ISM")])
    assert c.next_event_after(at(12, 30)).title == "ISM"
    assert c.next_event_after(at(9, 0)).title == "NFP"
    assert c.next_event_after(at(23, 0)) is None


def test_empty_calendar_blocks_nothing():
    assert not N.empty_calendar().blocked_at(at(12, 30))
    assert N.empty_calendar().is_empty


def test_naive_timestamps_are_refused_not_assumed_utc():
    """Assuming UTC is how a broker-server timestamp silently shifts the window
    by the server's offset -- the exact defect the module docstring is about."""
    c = cal([ev(12, 30)])
    with pytest.raises(ValueError):
        c.blocked_at(datetime(2026, 9, 4, 12, 30))
    with pytest.raises(TypeError):
        c.blocked_at("2026-09-04T12:30:00Z")


def test_a_non_utc_aware_timestamp_is_converted_not_rejected():
    c = cal([ev(12, 30)])
    tokyo = timezone(timedelta(hours=9))
    assert c.blocked_at(datetime(2026, 9, 4, 21, 30, tzinfo=tokyo))   # == 12:30Z
    assert not c.blocked_at(datetime(2026, 9, 4, 23, 0, tzinfo=tokyo))  # == 14:00Z


# --- 1b. ForexFactory parsing ----------------------------------------------

def test_forexfactory_dst_offsets_land_on_the_right_utc_instant():
    """The same 08:30 New York release is 12:30Z in summer and 13:30Z in winter.

    Dropping the offset would misplace every window by an hour for half the
    year, and nothing in a report would show it.
    """
    summer = N.parse_forexfactory([
        {"title": "NFP", "country": "USD", "impact": "High",
         "date": "2026-09-04T08:30:00-04:00"}])
    winter = N.parse_forexfactory([
        {"title": "NFP", "country": "USD", "impact": "High",
         "date": "2026-01-09T08:30:00-05:00"}])
    assert summer[0].at == datetime(2026, 9, 4, 12, 30, tzinfo=UTC)
    assert winter[0].at == datetime(2026, 1, 9, 13, 30, tzinfo=UTC)


def test_forexfactory_accepts_a_trailing_z():
    got = N.parse_forexfactory([
        {"title": "X", "country": "USD", "impact": "High",
         "date": "2026-09-04T12:30:00Z"}])
    assert got[0].at == at(12, 30)


def test_unusable_rows_are_dropped_but_a_good_week_still_parses():
    """One malformed row is normal; it must not cost the whole calendar."""
    got = N.parse_forexfactory([
        {"title": "no date", "country": "USD", "impact": "High", "date": ""},
        {"title": "naive", "country": "USD", "impact": "High",
         "date": "2026-09-04T12:30:00"},
        {"title": "no currency", "country": "", "impact": "High",
         "date": "2026-09-04T12:30:00Z"},
        {"title": "bad impact", "country": "USD", "impact": "Whatever",
         "date": "2026-09-04T12:30:00Z"},
        "not a dict",
        {"title": "good", "country": "usd", "impact": "HIGH",
         "date": "2026-09-04T12:30:00Z"},
    ])
    assert [e.title for e in got] == ["good"]
    assert got[0].currency == "USD" and got[0].impact == "high"


def test_a_changed_payload_shape_raises_instead_of_reporting_no_events():
    """The most dangerous failure this feature has.

    An empty calendar means "nothing is scheduled", so a feed whose shape
    changed would tell the bot it was free to trade straight through NFP. It has
    to raise instead, which is what makes the live path fail closed.
    """
    with pytest.raises(N.NewsUnavailable):
        N.parse_forexfactory({"events": []})            # not a list
    with pytest.raises(N.NewsUnavailable):
        N.parse_forexfactory([{"headline": "renamed field", "ccy": "USD"}])
    # A genuinely empty week is NOT an error.
    assert N.parse_forexfactory([]) == []


# --- 1c. on-disk round trip ------------------------------------------------

def test_events_round_trip_through_csv_preserving_the_utc_instant(tmp_path):
    events = [ev(12, 30, title="NFP"), ev(14, 0, impact="medium", title="ISM"),
              ev(9, 0, currency="EUR", impact="low", title="Speech")]
    N.write_events(str(tmp_path), events)
    back = N.read_calendar(str(N.news_dir(str(tmp_path))),
                           impacts=N.IMPACTS, currencies=None)
    assert len(back) == 3
    assert {e.at for e in back.relevant()} == {e.at for e in events}


def test_rewriting_merges_and_dedups_instead_of_appending(tmp_path):
    """A refresh re-reads the same week, so an append would double every event.

    Keyed on the whole record, the same reasoning as `reconcile_trades()` keying
    its upsert on the deal ticket.
    """
    N.write_events(str(tmp_path), [ev(12, 30, title="NFP")])
    N.write_events(str(tmp_path), [ev(12, 30, title="NFP"),
                                   ev(14, 0, title="ISM")])
    back = N.read_calendar(str(N.news_dir(str(tmp_path))))
    assert sorted(e.title for e in back.relevant()) == ["ISM", "NFP"]


def test_events_are_sharded_by_iso_week(tmp_path):
    written = N.write_events(str(tmp_path),
                             [ev(12, 30), ev(12, 30, day=11)])
    assert len(written) == 2, "different ISO weeks should be different files"


def test_a_missing_calendar_raises_with_the_command_to_run(tmp_path):
    """Never a silent empty calendar: a filter reduced to 'no events' is a
    filter that is not running, and the message has to say how to fix it -- the
    same contract as DataUnavailable."""
    with pytest.raises(N.NewsUnavailable) as exc:
        N.read_calendar(str(tmp_path / "nope"))
    assert "news_feed" in str(exc.value)


# --- 2. the empty-calendar regression --------------------------------------

def _signals_over(kind, cfg, calendar=None, n=1400):
    """Every signal the strategy emits over a synthetic series."""
    df = make_ohlc(kind, n=n, seed=7)
    strat = NWEnvelopeStrategy(cfg, calendar=calendar)
    feats = strat.prepare(df)
    out = []
    for i in range(strat.warmup_bars(), len(df)):
        ctx = BarContext(
            index=i, time=df.index[i].to_pydatetime(),
            bar=Bar(df["open"].values[i], df["high"].values[i],
                    df["low"].values[i], df["close"].values[i], 0.0),
            features={k: float(feats[k].values[i]) for k in feats.columns},
            position=None, spec=SPEC,
        )
        for sig in strat.on_bar(ctx):
            out.append((i, sig.type, sig.reason, sig.sl_distance, sig.tp_distance))
    return out


@pytest.mark.parametrize("kind", ["trend", "range", "gap", "spike", "flat"])
def test_news_off_is_signal_identical_to_no_calendar_at_all(kind):
    """The regression that keeps `data/reports/` comparable.

    `news_enabled=False` is the default, so this is what every existing caller
    gets. If it ever diverges, every stored baseline silently stops being a
    baseline.
    """
    base = _signals_over(kind, NWConfig())
    with_field = _signals_over(kind, NWConfig(news_enabled=False),
                               calendar=cal([ev(12, 30)]))
    assert base == with_field


@pytest.mark.parametrize("kind", ["trend", "range", "spike"])
def test_an_empty_calendar_with_news_on_also_changes_nothing(kind):
    """news_enabled=True plus no events must be a no-op, NOT a total block.

    This is the research path's deliberate opposite of the live path: empty
    means "nothing scheduled". Failing closed here would silently delete every
    trade from a backtest and report the remainder as a result.
    """
    base = _signals_over(kind, NWConfig())
    empty_on = _signals_over(kind, NWConfig(news_enabled=True))
    assert base == empty_on


def test_the_strategy_needs_no_calendar_argument():
    """Constructing it the old way still works -- nothing existing had to change."""
    strat = NWEnvelopeStrategy(NWConfig())
    assert strat.calendar.is_empty


# --- 2b. the two strategy-level vetoes -------------------------------------

def _ctx(ts, position=None, close=100.0, features=None):
    f = {"out": 100.0, "upper": 110.0, "lower": 90.0, "mae": 3.0,
         "atr": 1.0, "prev_close": close}
    f.update(features or {})
    return BarContext(index=1000, time=ts,
                      bar=Bar(close, close + 1, close - 1, close, 0.0),
                      features=f, position=None if position is None else position,
                      spec=SPEC)


class _Pos(object):
    def __init__(self, side):
        self.side = side
        self.volume = 1.0
        self.entry_price = 100.0


def test_entry_is_vetoed_inside_the_window_and_allowed_outside_it():
    cfg = NWConfig(news_enabled=True, news_currencies=("USD",))
    strat = NWEnvelopeStrategy(cfg, calendar=cal([ev(12, 30)]))
    # close 85 is below the 90 lower band, so this would normally enter long.
    assert strat.on_bar(_ctx(at(11, 0), close=85.0))[0].type is SignalType.ENTER_LONG
    assert strat.on_bar(_ctx(at(12, 30), close=85.0)) == []
    assert strat.on_bar(_ctx(at(13, 0), close=85.0))[0].type is SignalType.ENTER_LONG


def test_flatten_beats_exit_at_mean():
    """Inside the window an open position leaves regardless of the centre line.

    Ordering matters: if exit_at_mean were checked first, a position that had
    not reached the mean would sit through the release, which is the half of the
    rule that is about HOLDING rather than entering.
    """
    cfg = NWConfig(news_enabled=True, news_currencies=("USD",), exit_at_mean=True)
    strat = NWEnvelopeStrategy(cfg, calendar=cal([ev(12, 30)]))
    pos = _Pos(Side.LONG)
    # close 95 is BELOW the centre line, so exit_at_mean alone would not fire.
    sigs = strat.on_bar(_ctx(at(12, 30), position=pos, close=95.0))
    assert [s.reason for s in sigs] == ["news_blackout"]
    assert sigs[0].type is SignalType.EXIT
    # Outside the window the old behaviour is untouched.
    assert strat.on_bar(_ctx(at(11, 0), position=pos, close=95.0)) == []
    got = strat.on_bar(_ctx(at(11, 0), position=pos, close=101.0))
    assert [s.reason for s in got] == ["cross_center"]


def test_flatten_fires_even_with_exit_at_mean_disabled():
    cfg = NWConfig(news_enabled=True, news_currencies=("USD",), exit_at_mean=False)
    strat = NWEnvelopeStrategy(cfg, calendar=cal([ev(12, 30)]))
    sigs = strat.on_bar(_ctx(at(12, 30), position=_Pos(Side.SHORT), close=95.0))
    assert [s.reason for s in sigs] == ["news_blackout"]


def test_a_news_blackout_exit_reaches_the_ledger_as_its_own_reason():
    """So `run_baseline`'s exit_reason breakdown can price the rule.

    The engine needed no change for this: exit_reason comes from Signal.reason.
    """
    class Blackout(NWEnvelopeStrategy):
        def warmup_bars(self):
            return 0

        def feature_names(self):
            return ["dummy"]

        def prepare(self, b):
            return pd.DataFrame({"dummy": np.zeros(len(b))}, index=b.index)

        def on_bar(self, ctx):
            from backend.core.types import Signal
            if ctx.position is None and ctx.index == 0:
                return [Signal(SignalType.ENTER_LONG, "test", ctx.bar.close,
                               sl_distance=50.0, tp_distance=50.0)]
            if ctx.position is not None and ctx.index == 2:
                return [Signal(SignalType.EXIT, "news_blackout", ctx.bar.close)]
            return []

    bs = bars([(100, 101, 99, 100), (100, 101, 99, 100),
               (100, 101, 99, 100), (103, 104, 102, 103)])
    eng = BacktestEngine(Blackout(NWConfig()), SPEC, costs=None,
                         cfg=BacktestConfig(initial_balance=1000.0, volume=1.0))
    res = eng.run(bs)
    assert list(res.ledger["exit_reason"]) == ["news_blackout"]


# --- 3. the live feed ------------------------------------------------------

FF_PAYLOAD = [
    {"title": "Non-Farm Employment Change", "country": "USD", "impact": "High",
     "date": "2026-09-04T08:30:00-04:00"},                  # == 12:30Z
    {"title": "ISM Services PMI", "country": "USD", "impact": "Medium",
     "date": "2026-09-04T14:00:00+00:00"},
    {"title": "ECB Press Conference", "country": "EUR", "impact": "High",
     "date": "2026-09-04T10:00:00+00:00"},
    {"title": "Fed Speech", "country": "USD", "impact": "Low",
     "date": "2026-09-04T18:00:00+00:00"},
]


def feed(payload=FF_PAYLOAD, fail=False, **kw):
    from backend.live.news_feed import NewsFeed

    def opener(url):
        if fail:
            raise OSError("simulated outage")
        return payload

    kw.setdefault("write_archive", False)
    return NewsFeed(urls=("stub://calendar",), opener=opener, **kw)


def fetched(payload=FF_PAYLOAD, when=None, **kw):
    """A feed that has already refreshed, with the fetch clock PINNED.

    Pinned because staleness is measured against the fetch time: with the real
    clock, whether a fixed test instant counted as stale would depend on the day
    the suite ran.

    `max_age_seconds` defaults to effectively infinite so that a test about
    WINDOWS is not also a test about staleness -- the fixed instants below run
    to 18:00, which would otherwise age past the real 90-minute limit and make
    every one of them pass for the wrong reason. The staleness tests set it
    explicitly.
    """
    kw.setdefault("max_age_seconds", 10 ** 9)
    f = feed(payload=payload, **kw)
    f.refresh_once(now=when or at(9, 0))
    return f


def test_a_known_event_blocks_entries_and_flattens():
    v = fetched().verdict("XAUUSDm", at(12, 31))
    assert v.allow_entries is False
    assert v.flatten is True
    assert "Non-Farm" in v.detail


def test_outside_every_window_everything_is_allowed():
    v = fetched().verdict("XAUUSDm", at(11, 0))
    assert v.allow_entries is True and v.flatten is False and v.detail is None


def test_the_currency_filter_applies_per_symbol():
    """The EUR press conference must not black out a USD-configured symbol."""
    f = fetched()
    assert f.verdict("XAUUSDm", at(10, 0)).allow_entries is True
    assert f.verdict("BTCUSDm", at(14, 0)).allow_entries is False


def test_low_impact_events_are_archived_but_do_not_block():
    f = fetched()
    assert f.verdict("XAUUSDm", at(18, 0)).allow_entries is True


def test_before_the_first_fetch_entries_are_blocked_but_nothing_is_flattened():
    """The boot case of fail-closed.

    Blocking entries is the instruction. NOT flattening is the important half:
    closing a live position because an HTTP request has not completed yet would
    be a bug wearing a safety feature's clothes.
    """
    f = feed()
    v = f.verdict("XAUUSDm", at(12, 31))
    assert v.allow_entries is False
    assert v.flatten is False
    assert "no news calendar yet" in v.detail


def test_a_stale_calendar_blocks_entries_without_flattening():
    f = fetched(max_age_seconds=5400)
    v = f.verdict("XAUUSDm", at(9, 0) + timedelta(days=2))
    assert v.allow_entries is False
    assert v.flatten is False
    assert "last updated" in v.detail


def test_a_transient_failure_does_not_block_while_inside_the_grace_window():
    """Staleness closes the gate, not one failed request.

    A single 500 halting the strategy for 15 minutes would make the filter more
    dangerous than the releases it avoids. BOT_NEWS_MAX_AGE_MIN=0 is the strict
    reading if that is what someone wants.
    """
    f = fetched(max_age_seconds=5400)
    f._opener = lambda url: (_ for _ in ()).throw(OSError("down"))
    with pytest.raises(N.NewsUnavailable):
        f.refresh_once()
    # 30 minutes after the pinned fetch, against a 90-minute limit.
    assert f.verdict("XAUUSDm", at(9, 30)).allow_entries is True
    # ...and past the limit it closes, without ever flattening.
    late = f.verdict("XAUUSDm", at(11, 0))
    assert late.allow_entries is False and late.flatten is False


def test_a_failed_refresh_never_replaces_a_good_calendar_with_an_empty_one():
    """The dangerous failure: an empty calendar reads as 'nothing scheduled'."""
    f = fetched()
    before = len(f._calendar)
    f._opener = lambda url: (_ for _ in ()).throw(OSError("down"))
    with pytest.raises(N.NewsUnavailable):
        f.refresh_once()
    assert len(f._calendar) == before
    assert f.verdict("XAUUSDm", at(12, 31)).flatten is True


def test_a_first_fetch_that_fails_leaves_the_feed_closed():
    f = feed(fail=True)
    with pytest.raises(N.NewsUnavailable):
        f.refresh_once()
    v = f.verdict("XAUUSDm", at(11, 0))
    assert v.allow_entries is False and v.flatten is False
    assert "simulated outage" in v.detail


def test_start_survives_a_dead_provider_and_stop_is_clean():
    """`start()` must never raise: it runs inside BotManager.__init__, and an
    exception there would take the whole API down over a news feed."""
    f = feed(fail=True, refresh_seconds=3600)
    f.start()
    try:
        assert f.verdict("XAUUSDm", at(11, 0)).allow_entries is False
    finally:
        f.stop()


def test_a_partial_source_failure_still_yields_a_calendar():
    """thisweek alone covers any window that can be open right now."""
    from backend.live.news_feed import NewsFeed

    def opener(url):
        if "nextweek" in url:
            raise OSError("404")
        return FF_PAYLOAD

    f = NewsFeed(urls=("stub://thisweek", "stub://nextweek"), opener=opener,
                 write_archive=False, max_age_seconds=10 ** 9)
    assert f.refresh_once(now=at(9, 0)) == 4
    assert f.verdict("XAUUSDm", at(12, 31)).flatten is True


def test_the_verdict_cannot_suspend_position_management():
    """S7 requires manage_position() to run every cycle, blackout or not.

    A live position with no break-even stop through the most volatile hour of
    the day is worse than the entry the rule declined. Pinned as a shape check
    because the danger is a FUTURE field like `freeze_management` being added
    and quietly honoured by the loop.
    """
    from dataclasses import fields

    from backend.live.news_feed import NewsVerdict
    assert {f.name for f in fields(NewsVerdict)} == {
        "allow_entries", "flatten", "detail"}


def test_the_null_feed_never_blocks():
    from backend.live.news_feed import NullNewsFeed

    v = NullNewsFeed().verdict("XAUUSDm", at(12, 31))
    assert v.allow_entries is True and v.flatten is False and v.detail is None
    assert NullNewsFeed().status()["enabled"] is False


def test_bot_news_enabled_off_yields_the_null_feed(monkeypatch):
    from backend.live.news_feed import NewsFeed, NullNewsFeed, from_env

    monkeypatch.setenv("BOT_NEWS_ENABLED", "0")
    assert isinstance(from_env(), NullNewsFeed)
    monkeypatch.setenv("BOT_NEWS_ENABLED", "1")
    assert isinstance(from_env(), NewsFeed)


def test_env_overrides_reach_the_feed(monkeypatch):
    from backend.live.news_feed import from_env

    monkeypatch.setenv("BOT_NEWS_ENABLED", "1")
    monkeypatch.setenv("BOT_NEWS_BEFORE_MIN", "45")
    monkeypatch.setenv("BOT_NEWS_AFTER_MIN", "15")
    monkeypatch.setenv("BOT_NEWS_MAX_AGE_MIN", "0")
    monkeypatch.setenv("BOT_NEWS_IMPACTS", "high")
    f = from_env()
    assert (f.before_minutes, f.after_minutes) == (45.0, 15.0)
    assert f.max_age_seconds == 0.0
    assert f.impacts == ("high",)


def test_a_garbled_env_value_falls_back_instead_of_crashing(monkeypatch):
    from backend.live.news_feed import DEFAULT_TIMEOUT_SECONDS, from_env

    monkeypatch.setenv("BOT_NEWS_ENABLED", "1")
    monkeypatch.setenv("BOT_NEWS_TIMEOUT_SEC", "not-a-number")
    assert from_env().timeout == DEFAULT_TIMEOUT_SECONDS


def test_status_reports_enough_to_tell_blocked_from_broken():
    f = fetched()
    st = f.status(symbol="XAUUSDm", now=at(11, 0))
    assert st["enabled"] is True
    assert st["events_fetched"] == 4, "all four rows were fetched"
    assert st["events"] == 3, "the low-impact row cannot black anything out"
    assert st["stale"] is False
    assert st["last_fetch"] is not None
    assert st["symbol"]["currencies"] == ["USD"]
    assert st["symbol"]["next_event"]["title"] == "Non-Farm Employment Change"


def test_the_archive_keeps_every_impact_level(tmp_path):
    """So a later backtest can pick its own thresholds rather than today's."""
    f = feed(write_archive=True, root=str(tmp_path))
    f.refresh_once(now=at(9, 0))
    back = N.read_calendar(N.news_dir(str(tmp_path)), impacts=N.IMPACTS,
                           currencies=None)
    assert sorted(e.impact for e in back.relevant()) == [
        "high", "high", "low", "medium"]
