"""ForexFactory economic calendar for the live bot -- the only networked module.

This is the LIVE half of the news blackout. The window arithmetic is not here;
it is in `backend/core/news.py`, which the backtest also uses, so the two paths
cannot disagree about what "30 minutes before" means. This module does three
things the offline half must never do: talk to the network, hold a clock, and
decide what to do when it cannot see the calendar.

WHY IT IS NOT IN `backend/data/`. `tests/test_db_invariants.py` treats
`backend/{backtest,strategy,indicators,data,core,scripts}` as the research stack
and CLAUDE.md requires that stack to run with nothing but `data/`. A network
import reachable from `NWEnvelopeStrategy` would make a backtest fail during an
outage, so the fetcher lives under `backend/live/` -- next to the intended
extraction of order-sending -- and only `bot_manager` imports it.

NO NEW DEPENDENCY. `urllib.request` from the standard library, not `requests`.
The trading host is pinned to Python 3.8.10 on Windows and installs from wheels
only (see requirements.txt), so every avoided dependency is one fewer thing that
cannot be installed there when it matters.

FAIL CLOSED. If the calendar cannot be seen, entries stop. That is the
instruction, and it has a real cost -- a provider outage halts the strategy --
so the reason is reported through `bot_snapshots.detail` and `GET /health`
rather than only to stdout. CLAUDE.md's standing warning is about a bot that
"reports Running and never trades"; this module can cause exactly that, so it
must always be able to say why.

What fails closed is ENTRIES ONLY. An unknown calendar never triggers a
flatten: closing a live position because an HTTP request failed would be a bug
wearing a safety feature's clothes. Flatten fires only on a KNOWN event.
"""

import datetime as _dt
import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from backend.core.news import (DEFAULT_IMPACTS, NewsCalendar, NewsEvent,
                               NewsUnavailable, empty_calendar,
                               parse_forexfactory, write_events)
from backend.core.symbols import (NEWS_AFTER_MINUTES, NEWS_BEFORE_MINUTES,
                                  NEWS_IMPACTS, news_currencies_for)

# ONLY thisweek. The `ff_calendar_nextweek.json` and `ff_calendar_lastweek.json`
# URLs that circulate alongside it both return 404 as of 2026-09-04 (verified
# against the live host), and a permanently failing source is worse than a
# missing one here: every refresh would log an error and leave `last_error`
# populated, so the one field an operator reads to tell "the feed is broken"
# from "a release is in progress" would always say broken.
#
# thisweek is sufficient for the rule as specified. The blackout only ever asks
# about now +/- 30 minutes, and ForexFactory's week rolls over before the
# Sunday open, so the window is always inside the file that is published.
# Add more via BOT_NEWS_URLS if they come back.
DEFAULT_URLS = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
)

# 30 minutes, chosen against the 90-minute staleness limit below: three attempts
# inside the grace window, so two consecutive failures still cost nothing.
#
# Not shorter, for a measured reason -- the host returned HTTP 429 (Too Many
# Requests) when probed twice within a minute on 2026-09-04. It is an unmetered
# public endpoint with no published rate limit, and under fail-closed a 429 is
# not a cosmetic problem: enough of them in a row and the bot stops opening
# trades. Polling harder also buys nothing, because the feed publishes days
# ahead and the blackout only asks about now +/- 30 minutes.
DEFAULT_REFRESH_SECONDS = 1800.0
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_AGE_SECONDS = 5400.0    # 90 min -- see `verdict`

_USER_AGENT = "nw-bot/1.0 (+economic calendar blackout)"


def _log(msg):
    # type: (str) -> None
    # Mirrors bot_manager.log()'s format without importing it: bot_manager
    # imports MetaTrader5, and this module has to stay importable (and
    # testable) on a host with no terminal.
    print("[%s] %s" % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def _utcnow():
    # type: () -> _dt.datetime
    """Host wall clock, as an AWARE UTC datetime.

    Deliberately not the live loop's `bar_time`, which is broker-server epoch
    seconds with no offset applied. A blackout is a wall-clock rule about an
    instant in the real world, and the broker's clock is not that instant. The
    cost of this choice is one deployment assumption: the trading host's clock
    must be NTP-correct, because a host 30 minutes out shifts every window by 30
    minutes. `datetime.utcnow()` is avoided because it returns a NAIVE value and
    `backend.core.news` refuses those on purpose.
    """
    return _dt.datetime.now(_dt.timezone.utc)


def _env_float(name, default):
    # type: (str, float) -> float
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        _log("news: ignoring %s=%r (not a number), using %g" % (name, raw, default))
        return default


def _env_flag(name, default=True):
    # type: (str, bool) -> bool
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class NewsVerdict(object):
    """What the live loop is allowed to do right now.

    Three fields and no more. In particular there is NOTHING here that could
    suspend `manage_position()`: the scale-out and the break-even stop must keep
    running through a blackout, because a live position with no break-even stop
    during the most volatile hour of the day is worse than the entry the rule
    declined to take. Rule S7 owns that, and this type cannot override it.
    """

    allow_entries: bool
    flatten: bool
    detail: Optional[str] = None

    @property
    def blocked(self):
        # type: () -> bool
        return not self.allow_entries


_ALLOW = NewsVerdict(allow_entries=True, flatten=False, detail=None)


class NullNewsFeed(object):
    """The "off" switch, as an object rather than a branch.

    Used when BOT_NEWS_ENABLED=0 and as `TradingBot`'s default, so the live loop
    has exactly one code path and cannot grow a second one that forgets to check
    whether the filter is on. Never blocks, never flattens.
    """

    enabled = False

    def start(self):
        # type: () -> None
        pass

    def stop(self):
        # type: () -> None
        pass

    def verdict(self, symbol, now=None):
        # type: (str, Optional[_dt.datetime]) -> NewsVerdict
        return _ALLOW

    def status(self):
        # type: () -> Dict
        return {"enabled": False, "reason": "BOT_NEWS_ENABLED is off"}


class NewsFeed(object):
    """Fetches the calendar on a background thread; answers from memory.

    ONE per process, shared by every bot thread -- not one per symbol. Two
    threads polling the same public endpoint would double the traffic to get
    identical answers, and the per-symbol part of the question (which currencies
    matter) is a filter over the same event list, applied in `verdict`.

    The refresh runs on its OWN thread rather than lazily inside the trading
    loop. A hung TCP connect inside that loop would stall `manage_position()`,
    which S7 requires to run every ~15s cycle because the break-even trigger is
    an intrabar event. Trading must never wait on an HTTP request.
    """

    def __init__(self, urls=DEFAULT_URLS, refresh_seconds=DEFAULT_REFRESH_SECONDS,
                 timeout=DEFAULT_TIMEOUT_SECONDS,
                 max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
                 before_minutes=NEWS_BEFORE_MINUTES,
                 after_minutes=NEWS_AFTER_MINUTES, impacts=NEWS_IMPACTS,
                 root="data", write_archive=True, opener=None):
        # type: (...) -> None
        self.enabled = True
        self.urls = tuple(urls)
        self.refresh_seconds = float(refresh_seconds)
        self.timeout = float(timeout)
        self.max_age_seconds = float(max_age_seconds)
        self.before_minutes = float(before_minutes)
        self.after_minutes = float(after_minutes)
        self.impacts = tuple(impacts) or DEFAULT_IMPACTS
        self.root = root
        self.write_archive = write_archive
        # Injected in tests so the suite never touches the network, exactly as
        # the rest of the suite runs with no MT5 and no Postgres.
        self._opener = opener or self._http_get

        self._lock = threading.Lock()
        # currencies=None: `verdict` narrows per symbol, and narrowing can only
        # remove events, so the shared calendar has to hold all of them.
        self._calendar = empty_calendar(
            before_minutes=self.before_minutes, after_minutes=self.after_minutes,
            impacts=self.impacts, currencies=None)
        self._fetched_at = None      # type: Optional[_dt.datetime]
        self._fetched_count = 0
        self._last_error = None      # type: Optional[str]
        self._narrowed = {}          # type: Dict[Tuple[str, ...], NewsCalendar]
        self._stop_event = threading.Event()
        self._thread = None          # type: Optional[threading.Thread]

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        # type: () -> None
        """Fetch once synchronously, then keep refreshing in the background.

        The first fetch is synchronous so that a bot started moments after boot
        does not spend its first cycles refusing to trade for lack of a calendar
        it could have had. It is still safe if it fails: the failure leaves
        `_fetched_at` unset, which is the fail-closed state, and the reason is
        recorded for the dashboard.
        """
        if self._thread is not None:
            return
        try:
            self.refresh_once()
        except Exception as exc:                 # never block startup
            _log("news: first fetch failed -- %r" % exc)
        self._thread = threading.Thread(target=self._loop, name="news-feed")
        self._thread.daemon = True                # S6: never block interpreter exit
        self._thread.start()

    def stop(self):
        # type: () -> None
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                _log("news: refresh thread did not stop within 5s")

    def _loop(self):
        # type: () -> None
        while not self._stop_event.wait(self.refresh_seconds):
            try:
                self.refresh_once()
            except Exception as exc:
                # Swallowed on purpose: a refresh failure must not kill the
                # thread, or the calendar would freeze at its last value and
                # then go stale with nothing left running to recover it.
                _log("news: refresh failed -- %r" % exc)

    # -- fetching -----------------------------------------------------------

    def _http_get(self, url):
        # type: (str) -> object
        req = urllib.request.Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))

    def refresh_once(self, now=None):
        # type: (Optional[_dt.datetime]) -> int
        """Fetch every URL, swap the calendar in, archive the events.

        Raises `NewsUnavailable` if NO url produced events, which leaves the
        previous calendar in place to age out via `max_age_seconds` rather than
        replacing it with an empty one. Replacing it would be the dangerous
        failure: an empty calendar reads as "nothing scheduled" and would let
        the bot trade straight through a release.

        A partial success IS accepted -- `thisweek` alone covers any window that
        can be open right now, so losing `nextweek` is not worth refusing a
        calendar over.

        `now` stamps the fetch and exists so tests can pin it: staleness is
        measured against it, and a suite that used the real clock would decide
        whether a fixed test instant was stale based on the day it ran.
        """
        events = []    # type: List[NewsEvent]
        errors = []    # type: List[str]
        for url in self.urls:
            try:
                events.extend(parse_forexfactory(self._opener(url)))
            except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                    ValueError, NewsUnavailable) as exc:
                errors.append("%s: %r" % (url, exc))

        if not events:
            self._note_error("; ".join(errors) or "no events returned")
            raise NewsUnavailable("news refresh produced no events -- %s"
                                  % ("; ".join(errors) or "empty response"))

        unique = sorted(set(events), key=lambda e: (e.at, e.currency, e.title))
        calendar = NewsCalendar(
            unique, before_minutes=self.before_minutes,
            after_minutes=self.after_minutes, impacts=self.impacts,
            currencies=None)

        with self._lock:
            self._calendar = calendar
            self._fetched_at = now or _utcnow()
            self._fetched_count = len(unique)
            self._last_error = "; ".join(errors) or None
            self._narrowed = {}      # the per-symbol views belong to this fetch

        if errors:
            _log("news: refreshed with %d of %d sources (%s)"
                 % (len(self.urls) - len(errors), len(self.urls),
                    "; ".join(errors)))

        if self.write_archive:
            try:
                # EVERY parsed event is archived, including the low-impact and
                # holiday rows this filter ignores, so a later backtest can pick
                # its own thresholds instead of being stuck with today's.
                write_events(self.root, unique)
            except Exception as exc:
                # Archiving is history, not safety. A read-only disk must not
                # stop the bot from knowing when the next release is.
                _log("news: could not archive the calendar -- %r" % exc)
        return len(unique)

    def _note_error(self, detail):
        # type: (str) -> None
        with self._lock:
            self._last_error = detail

    # -- queries ------------------------------------------------------------

    def _calendar_for(self, symbol):
        # type: (str) -> NewsCalendar
        """The shared calendar narrowed to this symbol's currencies, memoised.

        Memoised per fetch because narrowing re-sorts and re-merges ~2,000
        events, and `verdict` is called about four times a minute per bot.
        """
        currencies = news_currencies_for(symbol)
        key = tuple(currencies)
        with self._lock:
            cached = self._narrowed.get(key)
            if cached is not None:
                return cached
            narrowed = self._calendar.for_currencies(currencies or None)
            self._narrowed[key] = narrowed
            return narrowed

    def verdict(self, symbol, now=None):
        # type: (str, Optional[_dt.datetime]) -> NewsVerdict
        """May this bot open a trade, and must it close what it holds?

        Staleness, not an individual failed request, is what closes the gate.
        `max_age_seconds` defaults to 90 minutes, so a transient 500 or a
        dropped connection does not halt the strategy, but a calendar we can no
        longer vouch for does. Set BOT_NEWS_MAX_AGE_MIN=0 for the strictest
        reading, where any failed refresh blocks immediately.

        Note what `flatten` is NOT set by: an unknown or stale calendar blocks
        entries and leaves open positions alone. Only a known event closes a
        position.
        """
        now = now or _utcnow()
        with self._lock:
            fetched_at = self._fetched_at
            last_error = self._last_error

        if fetched_at is None:
            return NewsVerdict(
                allow_entries=False, flatten=False,
                detail="not opening trades: no news calendar yet%s"
                       % (" (%s)" % last_error if last_error else ""))

        age = (now - fetched_at).total_seconds()
        if age > self.max_age_seconds:
            return NewsVerdict(
                allow_entries=False, flatten=False,
                detail="not opening trades: news calendar last updated %s "
                       "(%.0f min ago, limit %.0f)%s"
                       % (fetched_at.strftime("%H:%MZ"), age / 60.0,
                          self.max_age_seconds / 60.0,
                          " -- %s" % last_error if last_error else ""))

        calendar = self._calendar_for(symbol)
        if calendar.blocked_at(now):
            event = calendar.active_event(now)
            return NewsVerdict(
                allow_entries=False, flatten=True,
                detail="news blackout: %s" % (event.label() if event
                                              else "scheduled release"))
        return _ALLOW

    def status(self, symbol=None, now=None):
        # type: (Optional[str], Optional[_dt.datetime]) -> Dict
        """Feed health for GET /health -- never raises."""
        now = now or _utcnow()
        with self._lock:
            fetched_at = self._fetched_at
            last_error = self._last_error
            total = len(self._calendar)
            fetched = self._fetched_count
        age = None if fetched_at is None else (now - fetched_at).total_seconds()
        out = {
            "enabled": True,
            "sources": list(self.urls),
            # Two counts, because they answer different questions: `fetched` is
            # "did the feed work", `events` is "how many could black us out".
            # One number would make a feed that returned nothing but low-impact
            # rows look identical to a feed that returned nothing.
            "events_fetched": fetched,
            "events": total,
            "impacts": list(self.impacts),
            "window_minutes": {"before": self.before_minutes,
                               "after": self.after_minutes},
            "last_fetch": None if fetched_at is None else fetched_at.isoformat(),
            "age_seconds": None if age is None else round(age, 1),
            "max_age_seconds": self.max_age_seconds,
            "stale": age is None or age > self.max_age_seconds,
            "last_error": last_error,
            "refresh_seconds": self.refresh_seconds,
        }
        if symbol is not None:
            calendar = self._calendar_for(symbol)
            upcoming = calendar.next_event_after(now)
            out["symbol"] = {
                "symbol": symbol,
                "currencies": list(news_currencies_for(symbol)),
                "relevant_events": len(calendar),
                "next_event": None if upcoming is None else {
                    "at": upcoming.at.isoformat(), "impact": upcoming.impact,
                    "currency": upcoming.currency, "title": upcoming.title,
                },
            }
        return out


def from_env(root="data"):
    # type: (str) -> object
    """Build the feed the live process should use, from the environment.

    Returns a `NullNewsFeed` when BOT_NEWS_ENABLED is off, so callers never
    branch on whether the filter exists.
    """
    if not _env_flag("BOT_NEWS_ENABLED", True):
        return NullNewsFeed()
    raw_urls = os.environ.get("BOT_NEWS_URLS", "").strip()
    urls = tuple(u.strip() for u in raw_urls.split(",") if u.strip()) \
        or DEFAULT_URLS
    raw_impacts = os.environ.get("BOT_NEWS_IMPACTS", "").strip()
    impacts = tuple(i.strip().lower() for i in raw_impacts.split(",")
                    if i.strip()) or NEWS_IMPACTS
    return NewsFeed(
        urls=urls,
        refresh_seconds=_env_float("BOT_NEWS_REFRESH_SEC",
                                   DEFAULT_REFRESH_SECONDS),
        timeout=_env_float("BOT_NEWS_TIMEOUT_SEC", DEFAULT_TIMEOUT_SECONDS),
        max_age_seconds=_env_float("BOT_NEWS_MAX_AGE_MIN",
                                   DEFAULT_MAX_AGE_SECONDS / 60.0) * 60.0,
        before_minutes=_env_float("BOT_NEWS_BEFORE_MIN", NEWS_BEFORE_MINUTES),
        after_minutes=_env_float("BOT_NEWS_AFTER_MIN", NEWS_AFTER_MINUTES),
        impacts=impacts,
        root=root,
    )


def _cli(argv=None):
    # type: (Optional[Sequence[str]]) -> int
    """`python -m backend.live.news_feed --fetch` -- fetch, archive, report.

    Exists so the feed can be verified on its own, before a bot is started
    against it: this is the one step of the whole feature that touches the
    network, and a 403 or a changed payload shape should be discovered here
    rather than as a bot that quietly refuses to trade.
    """
    import argparse

    p = argparse.ArgumentParser(prog="backend.live.news_feed")
    p.add_argument("--fetch", action="store_true",
                   help="fetch the calendar now and write it under <root>/news/")
    p.add_argument("--root", default="data")
    p.add_argument("--symbol", default=None,
                   help="also report this symbol's next relevant event")
    p.add_argument("--no-archive", action="store_true",
                   help="fetch and report without writing any file")
    args = p.parse_args(argv)

    feed = NewsFeed(root=args.root, write_archive=not args.no_archive)
    try:
        count = feed.refresh_once()
    except NewsUnavailable as exc:
        print("FAILED: %s" % exc)
        return 1
    print("fetched %d events from %d source(s)" % (count, len(feed.urls)))

    now = _utcnow()
    status = feed.status(symbol=args.symbol, now=now)
    print(json.dumps(status, indent=2))
    if args.symbol:
        print("verdict for %s: %r" % (args.symbol, feed.verdict(args.symbol, now)))
    return 0


if __name__ == "__main__":               # pragma: no cover
    raise SystemExit(_cli())
