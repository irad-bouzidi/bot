"""Economic-calendar blackout windows -- the shared, offline half of the rule.

The bot must not open a position, and must not hold one, within a window around
a news release. This module owns the whole definition of that window and nothing
else: what an event is, which events count, and whether a given instant falls
inside one.

It deliberately knows NOTHING about where the events came from. Fetching them is
`backend/live/news_feed.py`, which needs the network; replaying them is
`backend/scripts/run_baseline.py`, which must not touch it. Both read the window
arithmetic from here so the live bot and the backtest cannot disagree about what
"30 minutes before" means -- the same reason `SYMBOL_CONFIG` lives in
`backend/core/symbols.py` rather than in `bot_manager`.

IMPORTS. Standard library only, and specifically NO `urllib`/`http`/`socket`, no
`MetaTrader5` and no `backend.db`. `tests/test_db_invariants.py` asserts that by
parsing this file: `backend/core` is on the research stack's import path, and
`CLAUDE.md` requires the research stack to run with nothing but `data/`. A
network call reachable from `NWEnvelopeStrategy` would make a backtest depend on
an outage.

TIME. Every `NewsEvent.at` is a TIMEZONE-AWARE datetime in UTC, and every query
timestamp must be too. This is not fussiness. The live loop's `bar_time` is
broker-SERVER epoch seconds with no offset applied (`bot_manager.py`), cached
bars are true UTC (`backend/data/cache.py` writes `+00:00` literally), and
ForexFactory stamps its events with a US-Eastern offset that changes twice a
year. Three clocks, so a naive datetime anywhere here would be a silent
30-minutes-to-5-hours error in the window -- which is the entire quantity being
computed. `_as_utc()` refuses naive input rather than assuming.
"""

import bisect
import csv
import datetime as _dt
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from .errors import BotError


# ForexFactory's own vocabulary, lowercased. "holiday" is carried through
# parsing so that a shape change in the feed stays distinguishable from a quiet
# week (see `parse_forexfactory`), but it is not in DEFAULT_IMPACTS: a bank
# holiday is a thin-liquidity day, not a release at a known instant, so blacking
# out 30 minutes around whatever hour the feed stamped on it would be arbitrary.
IMPACTS = ("high", "medium", "low", "holiday")

DEFAULT_IMPACTS = ("high", "medium")
DEFAULT_BEFORE_MINUTES = 30.0
DEFAULT_AFTER_MINUTES = 30.0

CSV_COLUMNS = ("at_utc", "currency", "impact", "title")

NEWS_DIRNAME = "news"


class NewsUnavailable(BotError):
    """The calendar could not be produced, so no window can be computed.

    Raised by the parser and by the fetcher, and it is why the live gate fails
    CLOSED. An empty calendar and an unavailable one have the same shape -- zero
    events -- but the opposite meaning: one says "nothing is scheduled, trade",
    the other says "we cannot see the schedule, do not trade". Collapsing them
    would trade blind through a release every time the feed changed shape, so
    they are different types rather than a flag on one.
    """


def _as_utc(ts, what="timestamp"):
    # type: (_dt.datetime, str) -> _dt.datetime
    """Require an aware datetime; normalise it to UTC.

    Refuses naive input instead of assuming UTC. Assuming is how a broker-server
    timestamp (see the module docstring) ends up compared against a true-UTC
    event time, shifting every window by the server's offset with nothing to
    show that it happened.
    """
    if not isinstance(ts, _dt.datetime):
        raise TypeError("%s must be a datetime, got %r"
                        % (what, type(ts).__name__))
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(
            "%s must be timezone-aware; got naive %r. Use "
            "datetime.now(timezone.utc), not datetime.utcnow()." % (what, ts))
    return ts.astimezone(_dt.timezone.utc)


@dataclass(frozen=True)
class NewsEvent(object):
    """One scheduled release.

    Frozen so a calendar cannot be mutated out from under a running backtest,
    and so events are hashable -- `write_events` dedups on the whole record.
    """

    at: _dt.datetime      # tz-aware, UTC
    currency: str         # "USD", "EUR", ... uppercased
    impact: str           # lowercased, one of IMPACTS
    title: str

    def label(self):
        # type: () -> str
        """One short line for a dashboard card or a log."""
        return "%s %s %s" % (self.currency, self.title,
                             self.at.strftime("%H:%MZ"))


class NewsCalendar(object):
    """A filtered event list plus the blackout windows it implies.

    Construction does the filtering (impact, currency) ONCE and precomputes
    MERGED windows, for two separate reasons:

      * Correctness. Two releases 20 minutes apart imply one continuous
        50-minute blackout, not two windows that toggle trading back on in the
        gap between them. Merging makes that structural instead of something
        every caller has to remember.
      * Cost. `blocked_at` is called once per bar, and a year of M5 gold is
        ~75,000 bars against ~2,000 relevant events. Scanning the event list per
        bar is 150M comparisons; a bisect over merged intervals is ~17.
    """

    def __init__(self, events, before_minutes=DEFAULT_BEFORE_MINUTES,
                 after_minutes=DEFAULT_AFTER_MINUTES,
                 impacts=DEFAULT_IMPACTS, currencies=None):
        # type: (Iterable[NewsEvent], float, float, Sequence[str], Optional[Sequence[str]]) -> None
        before = float(before_minutes)
        after = float(after_minutes)
        if before < 0 or after < 0:
            raise ValueError("news window minutes must not be negative, got "
                             "before=%r after=%r"
                             % (before_minutes, after_minutes))
        self.before = _dt.timedelta(minutes=before)
        self.after = _dt.timedelta(minutes=after)
        self.impacts = tuple(sorted(set(str(i).strip().lower() for i in impacts)))
        # None means "any currency". An EMPTY tuple is the opposite -- it matches
        # nothing -- so the two are kept distinct rather than normalised
        # together: `currencies=[]` from a mis-read config must black out
        # nothing loudly, not everything silently.
        self.currencies = (None if currencies is None
                           else tuple(sorted(set(str(c).strip().upper()
                                                 for c in currencies))))

        kept = []
        for ev in events:
            if ev.impact not in self.impacts:
                continue
            if self.currencies is not None and ev.currency not in self.currencies:
                continue
            kept.append(ev)
        kept.sort(key=lambda e: (e.at, e.currency, e.title))
        self._events = tuple(kept)                        # type: Tuple[NewsEvent, ...]
        self._event_times = [e.at for e in self._events]   # for bisect below
        self._starts, self._ends = self._merge_windows(self._events)

    def _merge_windows(self, events):
        # type: (Sequence[NewsEvent]) -> Tuple[List[_dt.datetime], List[_dt.datetime]]
        starts = []  # type: List[_dt.datetime]
        ends = []    # type: List[_dt.datetime]
        for ev in events:                      # already sorted by `at`
            lo = ev.at - self.before
            hi = ev.at + self.after
            if ends and lo <= ends[-1]:
                if hi > ends[-1]:
                    ends[-1] = hi
            else:
                starts.append(lo)
                ends.append(hi)
        return starts, ends

    # -- queries ------------------------------------------------------------

    def blocked_at(self, ts):
        # type: (_dt.datetime) -> bool
        """Is `ts` inside a blackout window?

        HALF-OPEN: `at - before <= ts < at + after`. The closed lower edge means
        a bar landing exactly on T-30 is already blocked; the open upper edge
        means T+30 is tradeable again, so a 30/30 window is exactly 60 minutes
        and two adjacent windows cannot both claim the same instant.
        """
        ts = _as_utc(ts)
        if not self._starts:
            return False
        i = bisect.bisect_right(self._starts, ts) - 1
        if i < 0:
            return False
        return ts < self._ends[i]

    def active_event(self, ts):
        # type: (_dt.datetime) -> Optional[NewsEvent]
        """The nearest relevant event whose own window covers `ts`, if any.

        Windows are merged for the blocked/not-blocked decision, but a human
        reading "why is the bot not trading" wants the release, so this goes back
        to the unmerged list. Nearest-by-time, because a cluster's windows
        overlap and the closest release is the one worth naming.
        """
        ts = _as_utc(ts)
        if not self._events:
            return None
        # Anything that could cover `ts` starts no later than ts + before and no
        # earlier than ts - after.
        lo = bisect.bisect_left(self._event_times, ts - self.after)
        hi = bisect.bisect_right(self._event_times, ts + self.before)
        best = None
        best_gap = None
        for ev in self._events[lo:hi]:
            if not (ev.at - self.before <= ts < ev.at + self.after):
                continue
            gap = abs(ev.at - ts)
            if best_gap is None or gap < best_gap:
                best, best_gap = ev, gap
        return best

    def next_event_after(self, ts):
        # type: (_dt.datetime) -> Optional[NewsEvent]
        ts = _as_utc(ts)
        i = bisect.bisect_right(self._event_times, ts)
        return self._events[i] if i < len(self._events) else None

    def window_of(self, event):
        # type: (NewsEvent) -> Tuple[_dt.datetime, _dt.datetime]
        return event.at - self.before, event.at + self.after

    def relevant(self):
        # type: () -> Tuple[NewsEvent, ...]
        """The events that survived the impact/currency filter."""
        return self._events

    def for_currencies(self, currencies):
        # type: (Optional[Sequence[str]]) -> NewsCalendar
        """A narrowed view sharing this calendar's window and impact settings.

        The live feed holds ONE calendar and every bot thread asks it about its
        own symbol, so the currency filter is applied per query rather than baked
        in at construction. Narrowing can only ever remove events, so build the
        parent with `currencies=None` if you intend to call this.
        """
        return NewsCalendar(self._events,
                            before_minutes=self.before.total_seconds() / 60.0,
                            after_minutes=self.after.total_seconds() / 60.0,
                            impacts=self.impacts, currencies=currencies)

    @property
    def is_empty(self):
        # type: () -> bool
        return not self._events

    def __len__(self):
        # type: () -> int
        return len(self._events)

    def __repr__(self):
        # type: () -> str
        return ("NewsCalendar(%d events, -%g/+%gmin, impacts=%s, currencies=%s)"
                % (len(self._events), self.before.total_seconds() / 60.0,
                   self.after.total_seconds() / 60.0, ",".join(self.impacts),
                   "any" if self.currencies is None
                   else ",".join(self.currencies)))


def empty_calendar(**kwargs):
    # type: (...) -> NewsCalendar
    """A calendar with no events -- the research path's default.

    Distinct from `NewsUnavailable`: this one means "nothing is scheduled", so
    the strategy behaves exactly as it did before the rule existed. That is what
    keeps every stored report in `data/reports/` valid.
    """
    return NewsCalendar([], **kwargs)


# -- ForexFactory parsing ---------------------------------------------------

def _parse_iso(raw):
    # type: (str) -> Optional[_dt.datetime]
    """ForexFactory's `date`, e.g. "2026-09-04T12:30:00-04:00".

    `datetime.fromisoformat` handles the +HH:MM/-HH:MM offset on Python 3.8, so
    no `dateutil`. It does NOT handle a trailing "Z", which the feed has been
    seen to use, so that is translated first. A naive result is rejected rather
    than assumed UTC: the offset is the only thing that distinguishes 12:30
    Eastern from 12:30 UTC, and guessing wrong misplaces the window by hours.
    """
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed.astimezone(_dt.timezone.utc)


def parse_forexfactory(payload):
    # type: (object) -> List[NewsEvent]
    """Turn one ForexFactory JSON response into events.

    The endpoint is public but undocumented and unversioned, so this is written
    to FAIL rather than to cope. A shape change that silently yielded zero
    events would produce an empty calendar, an empty calendar means "nothing
    scheduled", and "nothing scheduled" means the bot trades straight through
    NFP -- the exact outcome the rule exists to prevent. So:

      * a payload that is not a list raises;
      * a non-empty payload from which nothing at all could be parsed raises;
      * individual unusable rows (no parseable timestamp, blank currency,
        unrecognised impact) are dropped, because a single malformed row in an
        otherwise good week is a normal occurrence and must not halt trading.

    Rows whose impact is "holiday" ARE returned -- dropping them here would make
    a holiday-only week indistinguishable from a broken feed. DEFAULT_IMPACTS
    excludes them at the `NewsCalendar` level instead.
    """
    if not isinstance(payload, list):
        raise NewsUnavailable(
            "news feed returned %s, expected a JSON list of events"
            % type(payload).__name__)

    events = []  # type: List[NewsEvent]
    for row in payload:
        if not isinstance(row, dict):
            continue
        at = _parse_iso(row.get("date", ""))
        if at is None:
            continue
        # ForexFactory calls the currency column "country".
        currency = str(row.get("country", "") or "").strip().upper()
        impact = str(row.get("impact", "") or "").strip().lower()
        title = str(row.get("title", "") or "").strip() or "(untitled)"
        if not currency or impact not in IMPACTS:
            continue
        events.append(NewsEvent(at=at, currency=currency, impact=impact,
                                title=title))

    if payload and not events:
        raise NewsUnavailable(
            "news feed returned %d rows but none were parseable -- the feed's "
            "shape has probably changed. Refusing to report an empty calendar, "
            "which would read as 'nothing scheduled'." % len(payload))
    return events


# -- on-disk calendar -------------------------------------------------------

def news_dir(root="data"):
    # type: (str) -> str
    return os.path.join(root, NEWS_DIRNAME)


def shard_name(at):
    # type: (_dt.datetime) -> str
    """One file per ISO week, so a refresh rewrites only the current week."""
    iso = _as_utc(at).isocalendar()
    return "forexfactory-%04d-W%02d.csv" % (iso[0], iso[1])


def read_events(path):
    # type: (str) -> List[NewsEvent]
    """Read one calendar CSV. Unparseable rows are skipped, not fatal."""
    events = []  # type: List[NewsEvent]
    with open(path, "r", newline="") as fh:
        for row in csv.DictReader(fh):
            at = _parse_iso(row.get("at_utc", "") or "")
            if at is None:
                continue
            currency = str(row.get("currency", "") or "").strip().upper()
            impact = str(row.get("impact", "") or "").strip().lower()
            title = str(row.get("title", "") or "").strip() or "(untitled)"
            if not currency or impact not in IMPACTS:
                continue
            events.append(NewsEvent(at=at, currency=currency, impact=impact,
                                    title=title))
    return events


def read_calendar(path, **kwargs):
    # type: (str, ...) -> NewsCalendar
    """Load a calendar from a CSV file or a directory of them.

    Offline and network-free: this is the entry point `run_baseline` uses, and
    the research stack must keep running with nothing but `data/`. A missing
    path raises `NewsUnavailable` naming what was looked for, in the same spirit
    as `DataUnavailable` -- a filter silently reduced to "no events" is a filter
    that is not running.
    """
    if os.path.isdir(path):
        names = sorted(n for n in os.listdir(path) if n.lower().endswith(".csv"))
        if not names:
            raise NewsUnavailable("no calendar CSV files in %s" % path)
        events = []  # type: List[NewsEvent]
        for name in names:
            events.extend(read_events(os.path.join(path, name)))
    elif os.path.isfile(path):
        events = read_events(path)
    else:
        raise NewsUnavailable(
            "no news calendar at %s. Fetch one with: "
            "python -m backend.live.news_feed --fetch" % path)
    return NewsCalendar(events, **kwargs)


def write_events(root, events):
    # type: (str, Iterable[NewsEvent]) -> List[str]
    """Persist events under `<root>/news/`, one file per ISO week.

    MERGES with what is already on disk and dedups on the whole record, so a
    refresh corrects a moved event rather than appending a duplicate -- the same
    reasoning as `reconcile_trades()` keying its upsert on the deal ticket. The
    files accumulate the history that a live-API-only source otherwise never
    gives us, which is what a future backtest of this rule will read.

    Returns the paths written.
    """
    by_shard = {}  # type: dict
    for ev in events:
        by_shard.setdefault(shard_name(ev.at), set()).add(ev)

    if not by_shard:
        return []

    out_dir = news_dir(root)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    written = []  # type: List[str]
    for name, fresh in sorted(by_shard.items()):
        path = os.path.join(out_dir, name)
        merged = set(fresh)
        if os.path.isfile(path):
            merged.update(read_events(path))
        rows = sorted(merged, key=lambda e: (e.at, e.currency, e.title))
        tmp = path + ".tmp"
        with open(tmp, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_COLUMNS)
            for ev in rows:
                writer.writerow([ev.at.isoformat(), ev.currency, ev.impact,
                                 ev.title])
        # Write-then-replace: a half-written calendar read back by the next
        # `run_baseline` would silently shorten the blackout list.
        if os.path.exists(path):
            os.remove(path)
        os.rename(tmp, path)
        written.append(path)
    return written
