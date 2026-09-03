"""Connection pool and DSN resolution.

Two import rules, both load-bearing:

  * **No MetaTrader5.** Only `backend/bot_manager.py` and
    `backend/data/mt5_source.py` may import it (CLAUDE.md), and this module is
    imported by the API, by the bot threads and by the migration CLI.
  * **No psycopg2 at module scope.** The driver import is deferred into
    `_driver()`, so importing this module cannot fail on a host that has not
    installed it yet. `python -m pytest` has to keep passing with no server
    running, and the research path (run_baseline, the engine, the indicators)
    never touches Postgres at all.

The pool is threaded because the callers are: FastAPI answers `/stats` on its
own worker while each `TradingBot` daemon thread writes its own snapshot and
reconciles its own deals. One shared connection would interleave those
transactions.
"""

import os
import sys
import threading

from backend.core.errors import DatabaseUnavailable

# Matches docker-compose.yml, which publishes Postgres on loopback only.
# Read from the environment on every call rather than captured at import, so a
# test can point it at a scratch database with monkeypatch.setenv.
DEFAULT_URL = "postgresql://bot:bot@127.0.0.1:5432/tradingbot"

ENV_VAR = "BOT_DATABASE_URL"

# Named in every message that reports a connection or schema problem, so the
# fix never has to be guessed -- the same contract DataUnavailable has, where
# the error always carries the exact snapshot command to run.
_HINT = ("Start Postgres (docker compose up -d db) "
         "and apply the schema (python -m backend.db.migrate).")

_pool = None
_pool_url = None
_pool_lock = threading.RLock()


def database_url():
    # type: () -> str
    return os.environ.get(ENV_VAR) or DEFAULT_URL


def redact(url):
    # type: (str) -> str
    """Strip the password out of a DSN.

    Every DatabaseUnavailable message names the DSN so an operator can see
    which database was tried, and those messages reach the dashboard verbatim
    the way ConfigRejected's do. Without this the password would travel with
    them into the browser and the log.
    """
    if "@" not in url:
        return url
    head, _, tail = url.rpartition("@")
    scheme, sep, creds = head.partition("://")
    if not sep or ":" not in creds:
        return url
    user = creds.split(":", 1)[0]
    return "%s://%s:***@%s" % (scheme, user, tail)


def _driver():
    """Import psycopg2, turning a missing driver into the install command."""
    try:
        import psycopg2
        import psycopg2.extras
        import psycopg2.pool
    except ImportError as exc:
        raise DatabaseUnavailable(
            "The Postgres driver is not installed (%s). "
            "Run: pip install -r requirements.txt" % exc)
    return psycopg2


def get_pool():
    """The process-wide pool, created on first use.

    Re-created when the DSN changes under us, which is what lets a test swap
    databases without restarting the interpreter.
    """
    global _pool, _pool_url
    url = database_url()
    with _pool_lock:
        if _pool is not None and _pool_url == url:
            return _pool
        if _pool is not None:
            close_pool()
        psycopg2 = _driver()
        try:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                int(os.environ.get("BOT_DB_MIN_CONN", "1")),
                int(os.environ.get("BOT_DB_MAX_CONN", "8")),
                dsn=url,
                connect_timeout=int(os.environ.get("BOT_DB_CONNECT_TIMEOUT", "5")),
                application_name="nw-trading-bot",
            )
        except Exception as exc:
            _pool = None
            _pool_url = None
            raise DatabaseUnavailable(
                "Cannot connect to Postgres at %s: %s %s"
                % (redact(url), exc, _HINT))
        _pool_url = url
        return _pool


def close_pool():
    """Release every pooled connection. Called from the FastAPI shutdown hook."""
    global _pool, _pool_url
    with _pool_lock:
        if _pool is None:
            return
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None
        _pool_url = None


class _Connection(object):
    """Commits on success, rolls back on any exception, always returns the
    connection to the pool.

    psycopg2 leaves a failed transaction open, and every later statement on
    that connection then fails with InFailedSqlTransaction. Because the
    connection goes back to a *pool*, the next unrelated caller would inherit
    the poisoned one -- so one bad query in a bot thread would take out
    `/stats`. The rollback here is what confines the failure to its own call.
    """

    def __init__(self):
        self._pool = get_pool()
        self._conn = None

    def __enter__(self):
        try:
            self._conn = self._pool.getconn()
        except Exception as exc:
            raise DatabaseUnavailable(
                "No Postgres connection available: %s %s" % (exc, _HINT))
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        if self._conn is None:
            return False
        conn, self._conn = self._conn, None
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        except Exception:
            # A connection that cannot even roll back is broken. Close it so the
            # pool discards it rather than handing it to the next caller.
            try:
                conn.close()
            except Exception:
                pass
        finally:
            try:
                self._pool.putconn(conn)
            except Exception:
                pass
        return False


def connection():
    """`with connection() as conn:`"""
    return _Connection()


class _Cursor(object):
    """`with cursor() as cur:` -- a dict-row cursor on a committing connection.

    Rows are dicts because most are handed almost straight to the dashboard as
    JSON. A tuple cursor would put the SELECT's column order into the API
    contract, so reordering a column list would silently relabel the fields.
    """

    def __init__(self):
        self._ctx = _Connection()
        self._cur = None

    def __enter__(self):
        psycopg2 = _driver()
        conn = self._ctx.__enter__()
        try:
            self._cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        except Exception:
            self._ctx.__exit__(*sys.exc_info())
            raise
        return self._cur

    def __exit__(self, exc_type, exc, tb):
        if self._cur is not None:
            try:
                self._cur.close()
            except Exception:
                pass
            self._cur = None
        return self._ctx.__exit__(exc_type, exc, tb)


def cursor():
    """`with cursor() as cur:`"""
    return _Cursor()


def json_param(value):
    """Wrap a dict/list for a jsonb placeholder.

    psycopg2 ships no adapter for dict, so an unwrapped one fails at execute
    time with "can't adapt type 'dict'" rather than at review time.
    """
    psycopg2 = _driver()
    return psycopg2.extras.Json(value)


def ping():
    # type: () -> bool
    """True if the database answers. Never raises: callers use it to *report*
    the outage (GET /health, the boot banner) and must not become the outage."""
    try:
        with cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False
