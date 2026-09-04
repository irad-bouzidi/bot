"""The import boundaries the persistence layer has to keep. No database needed.

Three claims are made in CLAUDE.md and all three are cheap to break by accident:

  1. `backend/db/` never imports MetaTrader5, so it runs on the research machine
     and inside `python -m backend.db.migrate`.
  2. `backend/db/` never imports psycopg2 at module scope, so importing it on a
     host without the driver fails when you *use* it, with the install command,
     rather than at import.
  3. The research stack never imports `backend.db`, so `run_baseline`, the
     engine and the indicators keep working with nothing but `data/`.

Written as source/AST checks and subprocess imports rather than as prose in a
docstring, because a docstring does not fail a build.
"""

import ast
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(REPO_ROOT, "backend", "db")

# Everything that has to keep running with no MT5 and no Postgres. `backend/data`
# is in here deliberately: the bar cache is the handoff to the research machine,
# and giving it a database dependency would take the offline path with it.
RESEARCH_PACKAGES = ("backtest", "strategy", "indicators", "data", "core", "scripts")


def _python_files(directory):
    out = []
    for root, _dirs, files in os.walk(directory):
        if "__pycache__" in root:
            continue
        for name in files:
            if name.endswith(".py"):
                out.append(os.path.join(root, name))
    return out


def _imported_names(path):
    """Every module named by an `import` statement, with its line number."""
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno, node))
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.lineno, node))
    return found


def _is_module_scope(tree, target):
    """True if `target` is a direct child of the module body.

    An import inside a function is fine -- that is exactly how pool._driver()
    defers psycopg2. Only a top-level one breaks the guarantee.
    """
    return any(node is target for node in tree.body)


# ---------------------------------------------------------------------------

def test_the_db_package_does_not_import_metatrader5():
    """CLAUDE.md's MT5 invariant. `migrate` in particular has to run offline."""
    offenders = []
    for path in _python_files(DB_DIR):
        for name, lineno, _node in _imported_names(path):
            if name.split(".")[0] == "MetaTrader5":
                offenders.append("%s:%d" % (os.path.relpath(path, REPO_ROOT), lineno))
    assert not offenders, "backend/db must not import MetaTrader5: %s" % offenders


def test_psycopg2_is_never_imported_at_module_scope():
    """So that importing backend.db works on a host with no driver installed.

    pytest itself relies on this: tests/test_sizing_settings.py imports the
    repository to stub its cursor, and the suite has to pass with neither a
    driver nor a server.
    """
    offenders = []
    for path in _python_files(DB_DIR):
        with open(path) as fh:
            tree = ast.parse(fh.read(), filename=path)
        for name, lineno, node in _imported_names(path):
            if name.split(".")[0] != "psycopg2":
                continue
            if _is_module_scope(tree, node):
                offenders.append("%s:%d" % (os.path.relpath(path, REPO_ROOT), lineno))
    assert not offenders, (
        "psycopg2 must be imported inside a function (see pool._driver): %s"
        % offenders)


@pytest.mark.parametrize("package", RESEARCH_PACKAGES)
def test_the_research_stack_does_not_import_the_database(package):
    """The offline guarantee. A `backend.db` import anywhere in here would mean
    a baseline report needed a running Postgres."""
    directory = os.path.join(REPO_ROOT, "backend", package)
    if not os.path.isdir(directory):
        pytest.skip("no backend/%s" % package)
    offenders = []
    for path in _python_files(directory):
        for name, lineno, _node in _imported_names(path):
            if name == "backend.db" or name.startswith("backend.db."):
                offenders.append("%s:%d" % (os.path.relpath(path, REPO_ROOT), lineno))
    assert not offenders, (
        "backend/%s must stay runnable with no database: %s" % (package, offenders))


# The same offline guarantee, one step further: a backtest that cannot run
# without the internet is not reproducible, so nothing the research stack can
# reach may import a network library.
NETWORK_MODULES = ("urllib", "http", "socket", "requests", "httpx", "ssl",
                   "ftplib", "telnetlib", "xmlrpc")


@pytest.mark.parametrize("package", RESEARCH_PACKAGES)
def test_the_research_stack_does_not_import_the_network(package):
    directory = os.path.join(REPO_ROOT, "backend", package)
    if not os.path.isdir(directory):
        pytest.skip("no backend/%s" % package)
    offenders = []
    for path in _python_files(directory):
        for name, lineno, _node in _imported_names(path):
            if name.split(".")[0] in NETWORK_MODULES:
                offenders.append("%s:%d imports %s"
                                 % (os.path.relpath(path, REPO_ROOT), lineno, name))
    assert not offenders, (
        "backend/%s must stay runnable offline; put network code in "
        "backend/live/: %s" % (package, offenders))


def test_nothing_in_the_backend_reaches_the_network():
    """No module may. Stronger than it used to be, and deliberately so.

    This test previously allowed exactly one exception -- the ForexFactory
    calendar fetcher behind the news blackout. That rule is gone and so is the
    fetcher, which leaves the whole backend with no outbound dependency of any
    kind: the bot talks to MT5 over local IPC and to Postgres on loopback, and
    nothing else. Keeping the check rather than deleting it along with the module
    it was written for is what stops a networked import reappearing somewhere
    the offline research path can reach.

    If a networked feature is ever genuinely wanted, put it under
    `backend/live/` and re-add that one file here as an allowed exception --
    never widen this to a whole package.
    """
    networked = []
    backend_dir = os.path.join(REPO_ROOT, "backend")
    for path in _python_files(backend_dir):
        for name, _lineno, _node in _imported_names(path):
            if name.split(".")[0] in NETWORK_MODULES:
                networked.append(os.path.relpath(path, REPO_ROOT).replace("\\", "/"))
                break
    assert sorted(set(networked)) == [], (
        "no backend module may reach the network: %s" % sorted(set(networked)))


def test_the_db_package_imports_with_neither_driver_nor_server():
    """The end-to-end version of the two source checks above.

    MetaTrader5 and psycopg2 are both blanked in sys.modules, and the package
    still has to import and resolve a DSN.
    """
    code = (
        "import sys;"
        "sys.modules['MetaTrader5']=None;"
        "sys.modules['psycopg2']=None;"
        "from backend.db import pool, repository, migrate;"
        "print(pool.redact('postgresql://u:p@h:5432/d'))"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       cwd=REPO_ROOT)
    assert r.returncode == 0, r.stderr.decode()
    assert "***" in r.stdout.decode()


def test_a_missing_driver_reports_the_install_command():
    """The failure has to be actionable at the point of use.

    `DatabaseUnavailable` carries the fix for the same reason `DataUnavailable`
    carries the exact snapshot command: an error that only says "no" costs
    someone a debugging session.
    """
    code = (
        "import sys;"
        "sys.modules['psycopg2']=None;"
        "from backend.db import pool;"
        "from backend.core.errors import DatabaseUnavailable;"
        "\ntry:\n"
        "    pool.get_pool()\n"
        "except DatabaseUnavailable as exc:\n"
        "    print(exc)\n"
        "else:\n"
        "    raise SystemExit('expected DatabaseUnavailable')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       cwd=REPO_ROOT)
    assert r.returncode == 0, r.stderr.decode()
    assert "pip install -r requirements.txt" in r.stdout.decode()


def test_the_dsn_never_carries_its_password_into_a_message():
    """Every DatabaseUnavailable names the DSN so an operator can see which
    database was tried, and those messages reach the browser verbatim the way
    ConfigRejected's do."""
    from backend.db import pool

    assert pool.redact("postgresql://bot:s3cret@127.0.0.1:5432/tradingbot") == \
        "postgresql://bot:***@127.0.0.1:5432/tradingbot"
    # No password to hide, and nothing invented.
    assert pool.redact("postgresql://bot@127.0.0.1:5432/tradingbot") == \
        "postgresql://bot@127.0.0.1:5432/tradingbot"
    assert "s3cret" not in pool.redact(
        "postgresql://bot:s3cret@127.0.0.1:5432/tradingbot")


def test_the_schema_is_reachable_and_idempotent_in_shape():
    """schema.sql is read from disk at runtime, so a packaging slip that dropped
    it would only show up when someone tried to migrate."""
    from backend.db import repository

    sql = repository.schema_sql()
    # Every CREATE TABLE has to be re-runnable: the container applies this file
    # on a fresh volume, and `migrate` applies it to live databases.
    creates = [line for line in sql.splitlines()
               if line.strip().upper().startswith("CREATE TABLE")]
    assert creates, "no CREATE TABLE statements found"
    for line in creates:
        assert "IF NOT EXISTS" in line.upper(), line
    for line in sql.splitlines():
        if line.strip().upper().startswith("CREATE INDEX"):
            assert "IF NOT EXISTS" in line.upper(), line
    # ALTER TABLE too, and this is the path that actually matters on a live
    # deployment: CREATE TABLE IF NOT EXISTS is a NO-OP against an existing
    # table, so a column added for an already-running database arrives only
    # through an ALTER -- and one without IF NOT EXISTS makes the whole file
    # fail on its second application, leaving the column missing and the
    # migration reporting failure rather than the column's absence.
    #
    # Checked per STATEMENT, not per line: an ADD COLUMN routinely wraps onto
    # the next line, and a line-wise check would pass a bare `ALTER TABLE x`
    # only because the guard it was missing lived one line down.
    alters = [s for s in sql.split(";")
              if s.strip().upper().startswith("ALTER TABLE")]
    assert alters, "no ALTER TABLE statements found"
    for stmt in alters:
        assert "IF NOT EXISTS" in stmt.upper(), stmt.strip()


def test_reporting_failures_cannot_reach_the_trading_path():
    """A database or history failure must not stop a bot managing a position.

    The live loop calls `_reconcile_quietly`, never `reconcile_trades`, so that
    no failure in the reporting path can kill the thread or cost a cycle. The
    prologue call is the dangerous one: it runs before the `while`, so an
    exception there would end the thread and the bot would never trade.

    Checked at the source, because the alternative is a live-fire test against a
    real position.
    """
    mt5 = pytest.importorskip("MetaTrader5", reason="bot_manager imports MetaTrader5")
    from backend import bot_manager as bm

    src = open(os.path.join(REPO_ROOT, "backend", "bot_manager.py")).read()
    tree = ast.parse(src)

    run_body = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            run_body = node
            break
    assert run_body is not None, "TradingBot.run not found"

    bare = []
    for node in ast.walk(run_body):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "reconcile_trades":
            bare.append(node.lineno)
    assert not bare, (
        "run() must call _reconcile_quietly, not reconcile_trades, at lines %s"
        % bare)

    # And the wrapper really does swallow.
    class Boom(bm.TradingBot):
        def reconcile_trades(self, full=False):
            raise RuntimeError("postgres is on fire")

    bot = Boom.__new__(Boom)
    bot.symbol = "XAUUSDm"
    assert bot._reconcile_quietly() is False
    assert bot._reconcile_quietly(full=True) is False


def test_persist_never_raises_into_the_live_loop():
    """`_persist` is every DB write the bot threads make. A Postgres outage must
    cost the record and nothing else: halting would leave a real position with
    nothing to fire its scale-out or move its stop to break-even."""
    pytest.importorskip("MetaTrader5", reason="bot_manager imports MetaTrader5")
    from backend import bot_manager as bm

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    assert bm._persist("do a thing", boom) is False
    assert bm._persist("do a thing", lambda: None) is True
