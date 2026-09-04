import os
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from backend.bot_manager import (
    BotManager, SUPPORTED_SYMBOLS, log, scale_out_fraction, init_persistence,
    AUTO_RESUME,
)
from backend.core.errors import ConfigRejected, DatabaseUnavailable
from backend.db import pool as db_pool
from backend.db import repository as repo
import uvicorn
from datetime import datetime

# S2: this API can start and stop LIVE trading and has no authentication, so it
# must not be reachable from the network. Bind loopback and allow only the local
# dev frontend. Override deliberately via env if you know what you are doing.
HOST = os.environ.get("BOT_HOST", "127.0.0.1")
PORT = int(os.environ.get("BOT_PORT", "8000"))

# The containerised frontend is served as static files on :3000 and the BROWSER
# still calls this API on 127.0.0.1:8000 directly -- that container serves the
# page and does NOT proxy to the backend. That is deliberate: proxying would
# need this process reachable from the Docker bridge network, i.e. bound off
# loopback, which would put unauthenticated live-trading control on the host's
# network interface. Adding the container's published origin to CORS costs
# nothing by comparison. See README.md, "Why nothing proxies the API".
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "BOT_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if o.strip()
]

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

manager = BotManager()


@app.on_event("startup")
def _startup():
    """Connect to Postgres before serving anything, and refuse to boot without it.

    Refusing is the point. Postgres now holds `lot_size`, and `lot_size` is the
    only risk control this bot has -- no equity-based sizing, no daily loss cap,
    no margin check. Booting with the 0.1 code default because the database was
    unreachable would restore ~$70/trade for someone who had deliberately
    lowered it, which is precisely the silent restore the old settings file went
    to the trouble of a write-then-rename to prevent.
    """
    try:
        init_persistence()
    except DatabaseUnavailable as exc:
        # Logged before it propagates. uvicorn prints the traceback and then
        # "Application startup failed", which buries the one line that says what
        # to do -- and this is the error a first-time setup actually hits.
        log("=" * 72)
        log("REFUSING TO START: %s" % exc)
        log("Nothing was started and no orders were sent. `lot_size` lives in "
            "the database; starting on the code default would silently restore "
            "the risk of a size that was lowered on purpose.")
        log("=" * 72)
        raise
    log("db: connected to %s" % db_pool.redact(db_pool.database_url()))
    # Build the trade history before any bot is started. The old code only ever
    # learned about trades from a running thread, so a fresh process showed zero
    # trades until someone pressed Start -- on an account that may have been
    # trading for a year.
    manager.reconcile_all(full=True)
    manager.resume_desired_bots()


@app.on_event("shutdown")
def _shutdown():
    """S6: stop bots on exit. Previously the non-daemon threads kept a live
    position open and Ctrl+C on uvicorn would hang."""
    log("Shutting down: stopping all bots")
    manager.stop_all()
    db_pool.close_pool()

class SymbolControl(BaseModel):
    symbol: str
    action: str # "start" or "stop"

class SizingUpdate(BaseModel):
    """A sizing edit from the dashboard, in LOTS -- the unit the trader types.

    `scale_out_lots` is converted to the stored fraction in one place
    (scale_out_fraction); this model never carries a fraction. Either field may be
    omitted to leave that one alone.
    """
    symbol: str
    lot_size: Optional[float] = None
    scale_out_lots: Optional[float] = None

class BacktestSizing(BaseModel):
    """One symbol's sizing for one run, in LOTS. Never a fraction -- see
    scale_out_fraction()."""
    symbol: str
    lot_size: Optional[float] = None
    scale_out_lots: Optional[float] = None

class BacktestRequest(BaseModel):
    """One or several symbols, over one window, on ONE account.

    `symbols` is the field the dashboard sends; `symbol` is the single-symbol
    form every stored run and every older client used, and is still accepted.
    Sizing is per symbol because a lot is not comparable across symbols: 0.1 lots
    of gold and 0.1 of Bitcoin risk about the same $70 only by coincidence of the
    two contract sizes. The flat `lot_size` / `scale_out_lots` remain as the
    single-symbol shorthand and, for lack of anywhere better to put them, apply
    to every selected symbol when `sizing` is absent.
    """
    symbol: Optional[str] = None
    symbols: Optional[List[str]] = None
    start_date: str
    end_date: str
    initial_balance: float
    # Override the live sizing for this run only. Omitted -> whatever the bot is
    # currently configured with, so the default backtest matches the default bot.
    lot_size: Optional[float] = None
    scale_out_lots: Optional[float] = None
    sizing: Optional[List[BacktestSizing]] = None

class PreferencesUpdate(BaseModel):
    """A patch of the dashboard's own state -- theme, active view, form values.

    MERGED into the stored document, not replacing it: the theme switch and the
    backtest form both write here, and a replace would mean whichever fired last
    erased the other's fields.
    """
    data: Dict[str, Any]

@app.get("/health")
def health():
    """Is the database reachable and migrated? Never raises.

    Separate from /stats so the dashboard can tell "the backend is down" from
    "the backend is up and Postgres is not" -- two different things to go and
    fix, and the old single failing poll could not distinguish them.
    """
    reachable = db_pool.ping()
    version = 0
    complete = False
    if reachable:
        try:
            version = repo.schema_version()
            complete = repo.tables_present()
        except Exception as exc:
            log("health: schema check failed -- %r" % exc)
    return {
        "database": {
            "url": db_pool.redact(db_pool.database_url()),
            "reachable": reachable,
            "schema_version": version,
            "tables_present": complete,
            "migrate_command": "python -m backend.db.migrate",
        },
        "auto_resume": AUTO_RESUME,
        "symbols": SUPPORTED_SYMBOLS,
    }

@app.get("/stats")
def get_stats():
    return {
        "account": manager.get_account_info(),
        "bots": {
            symbol: manager.get_bot_stats(symbol)
            for symbol in SUPPORTED_SYMBOLS
        }
    }

@app.get("/equity")
def get_equity(limit: int = 500):
    """Balance/equity samples from `account_snapshots`, oldest first."""
    try:
        return {"points": manager.equity_curve(limit=min(max(limit, 1), 5000))}
    except DatabaseUnavailable as exc:
        return {"error": str(exc), "points": []}

@app.post("/control")
def control_bot(ctrl: SymbolControl):
    if ctrl.action == "start":
        ok = manager.start_bot(ctrl.symbol)
        if not ok:
            return {"error": "Bot only supports: %s" % ", ".join(SUPPORTED_SYMBOLS)}
        return {"message": "Bot started for %s" % ctrl.symbol}
    elif ctrl.action == "stop":
        manager.stop_bot(ctrl.symbol)
        return {"message": "Bot stopped for %s" % ctrl.symbol}
    # Recorded even though it is rejected: this endpoint is unauthenticated, so
    # a malformed request against it is worth a row.
    try:
        repo.record_control_event(ctrl.symbol, ctrl.action, False, "invalid action")
    except Exception:
        pass
    return {"error": "Invalid action"}

@app.get("/control/events")
def control_events(symbol: Optional[str] = None, limit: int = 50):
    try:
        return {"events": repo.list_control_events(symbol, min(max(limit, 1), 500))}
    except DatabaseUnavailable as exc:
        return {"error": str(exc), "events": []}

@app.get("/settings")
def get_settings():
    """Sizing for every configured symbol, in the lots the UI edits."""
    out = {}
    for symbol in SUPPORTED_SYMBOLS:
        try:
            out[symbol] = manager.get_settings(symbol)
        except ConfigRejected as exc:
            out[symbol] = {"error": str(exc)}
    return out

@app.post("/settings")
def update_settings(ctrl: SizingUpdate):
    """S2: this changes the size of REAL orders and is as unauthenticated as
    /control -- the loopback bind is what protects both. It is refused outright
    while a position is open; see BotManager.update_settings for why."""
    try:
        return manager.update_settings(ctrl.symbol, ctrl.lot_size, ctrl.scale_out_lots)
    except ConfigRejected as exc:
        return {"error": str(exc)}
    except DatabaseUnavailable as exc:
        # Refused, not applied-in-memory-only. A size the database would not
        # accept must not be traded for the rest of the process and then vanish
        # on restart.
        return {"error": "Sizing NOT changed -- %s" % exc}

@app.get("/settings/history")
def settings_history(symbol: Optional[str] = None, limit: int = 50):
    """Who changed the sizing, to what, and when. The JSON file overwrote this."""
    try:
        return {"history": repo.settings_history(symbol, min(max(limit, 1), 500))}
    except DatabaseUnavailable as exc:
        return {"error": str(exc), "history": []}

@app.get("/trades")
def list_trades(symbol: Optional[str] = None, status: Optional[str] = None,
                limit: int = 100, offset: int = 0):
    """This bot's trade history, one row per POSITION.

    Positions, not closing deals: a trade that scaled out and then stopped at
    break-even is ONE row with exit_count=2, not a win plus a flat. See
    repository._REBUILD_TRADES.
    """
    if status not in (None, "open", "closed"):
        return {"error": "status must be 'open' or 'closed'"}
    try:
        limit = min(max(limit, 1), 1000)
        return {
            "trades": repo.list_trades(symbol, status, limit, max(offset, 0)),
            "total": repo.count_trades(symbol, status),
            "limit": limit,
            "offset": max(offset, 0),
        }
    except DatabaseUnavailable as exc:
        return {"error": str(exc), "trades": [], "total": 0}

@app.get("/trades/{position_id}")
def trade_deals(position_id: int):
    """The raw deals behind one trade -- entry, any scale-out, and the exit."""
    try:
        return {"position_id": position_id, "deals": repo.list_deals(position_id)}
    except DatabaseUnavailable as exc:
        return {"error": str(exc), "deals": []}

@app.post("/trades/refresh")
def refresh_trades(symbol: Optional[str] = None, full: bool = False):
    """Re-read MT5's deal history into Postgres now, rather than on the next
    once-a-minute pass. Idempotent -- the upsert is keyed on the deal ticket."""
    if symbol and symbol not in SUPPORTED_SYMBOLS:
        # Checked here rather than left to TradingBot's own refusal, so the
        # message names the configured symbols instead of surfacing a raised
        # exception's repr.
        return {"error": "Unknown symbol %r. Configured: %s"
                         % (symbol, ", ".join(SUPPORTED_SYMBOLS))}
    try:
        if symbol:
            bot = manager.bots.get(symbol)
            if bot is None:
                from backend.bot_manager import TradingBot
                bot = TradingBot(symbol)
            return {"refreshed": {symbol: bot.reconcile_trades(full=full)}}
        return {"refreshed": manager.reconcile_all(full=full)}
    except Exception as exc:
        return {"error": str(exc)}

@app.post("/backtest")
def backtest(req: BacktestRequest):
    """Run the legacy engine over one or several symbols and STORE the run.

    Several symbols are replayed onto ONE account, in close-time order, because
    that is the only reading of "both combined" a trader can act on -- see
    combine_legacy_results. The combined drawdown is therefore the merged curve's
    and is NOT the per-symbol figures added up.

    Errored runs are stored too. A run that found no bars for its window is a
    fact about that window, and dropping it is how the same unavailable range
    gets asked for five times.
    """
    started = time.time()
    start = end = None
    symbols = []
    sizing = {}
    try:
        symbols = _requested_symbols(req)
        start = datetime.fromisoformat(req.start_date)
        end = datetime.fromisoformat(req.end_date)
        sizing = _resolve_sizing(req, symbols)
        result = manager.run_backtests(symbols, start, end, req.initial_balance,
                                       sizing=sizing)
    except Exception as exc:
        # ConfigRejected (unknown symbol, impossible scale-out), a bad ISO date,
        # and anything the engine throws all land here and are all stored: a run
        # that could not happen is still a fact about the inputs it was given.
        _store_backtest(req, symbols, start, end, sizing, None, str(exc), started)
        return {"error": str(exc)}

    # run_backtests reports "no bars in range" as an {"error": ...} body rather
    # than by raising, so the stored status has to come from the payload.
    error = result.get("error") if isinstance(result, dict) else None
    stored = _store_backtest(req, symbols, start, end, sizing, result, error, started)
    if error:
        return result
    if stored is not None:
        result = dict(result)
        result["run_id"] = stored["id"]
        result["created_at"] = stored["created_at"]
    return result


def _raw_symbols(req):
    """Whatever the request named, unvalidated and de-duplicated in order."""
    raw = list(req.symbols) if req.symbols else ([req.symbol] if req.symbol else [])
    out = []
    for symbol in raw:
        symbol = (symbol or "").strip()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _requested_symbols(req):
    """The symbols this run covers, validated and de-duplicated in order.

    Validated HERE rather than left to run_backtest, so that an unknown symbol in
    a combined request is refused before any of it runs -- a partial combined
    result is worse than none, because it looks like an answer to the question
    that was asked.
    """
    symbols = _raw_symbols(req)
    for symbol in symbols:
        if symbol not in SUPPORTED_SYMBOLS:
            raise ConfigRejected("Unknown symbol %r. Configured: %s"
                                 % (symbol, ", ".join(SUPPORTED_SYMBOLS)))
    if not symbols:
        raise ConfigRejected("Select at least one symbol. Configured: %s"
                             % ", ".join(SUPPORTED_SYMBOLS))
    return symbols


def _resolve_sizing(req, symbols):
    """{symbol: {"lot_size", "partial_fraction"}} for this run only.

    The scale-out arrives in LOTS and is converted against the lot size THIS RUN
    uses for THAT symbol -- not the live one, and not another symbol's. The pair
    only means anything read together: 0.05 out of 0.1 is half, and out of 0.2 it
    is a quarter, so resolving it against the wrong lot size silently backtests a
    different rule from the one the form is describing.
    """
    per_symbol = {}
    if req.sizing:
        for entry in req.sizing:
            if entry.symbol not in symbols:
                raise ConfigRejected(
                    "Sizing given for %r, which is not in this run." % entry.symbol)
            per_symbol[entry.symbol] = (entry.lot_size, entry.scale_out_lots)
    for symbol in symbols:
        # The flat fields are the single-symbol shorthand; applied to each symbol
        # only when no per-symbol entry overrides them.
        per_symbol.setdefault(symbol, (req.lot_size, req.scale_out_lots))

    out = {}
    for symbol in symbols:
        lot, scale_out = per_symbol[symbol]
        resolved = {}
        if lot is not None:
            resolved["lot_size"] = lot
        if scale_out is not None:
            live = lot if lot is not None else manager.get_settings(symbol)["lot_size"]
            resolved["partial_fraction"] = scale_out_fraction(live, scale_out)
        out[symbol] = resolved
    return out


def _run_label(symbols):
    """What `backtest_runs.symbol` shows for this run.

    Kept as a label rather than dropped, because every stored row has one and the
    list views read it; `symbols` is the column that gets queried.
    """
    return " + ".join(symbols) if symbols else "(none)"


def _store_backtest(req, symbols, start, end, sizing, result, error, started):
    """Record one run. Never raises -- a storage failure must not lose the run's
    RESULT, which the user is waiting on and which took real time to compute.

    `symbols` is empty when validation refused the request before resolving it,
    so what was ASKED FOR is stored instead. A run rejected for naming a symbol
    that does not exist is a fact about the request worth keeping, for the same
    reason a window with no bars is.
    """
    symbols = symbols or _raw_symbols(req)
    single = symbols[0] if len(symbols) == 1 else None
    stored_sizing = _stored_sizing(req, symbols, sizing)
    one = stored_sizing.get(single) or {} if single else {}
    try:
        return repo.record_backtest(
            symbol=_run_label(symbols),
            symbols=symbols,
            start_date=start or req.start_date,
            end_date=end or req.end_date,
            initial_balance=req.initial_balance,
            # Only meaningful for a single symbol. On a combined run they would
            # be one symbol's lots filed under both, so they stay NULL and
            # `sizing` carries the real per-symbol numbers.
            lot_size=one.get("lot_size"),
            scale_out_lots=one.get("scale_out_lots"),
            partial_fraction=one.get("partial_fraction"),
            sizing=stored_sizing,
            engine="legacy",
            status="error" if error else "ok",
            error=error,
            result=None if error else result,
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as exc:
        log("db: could not store the backtest run -- %r" % exc)
        return None


def _stored_sizing(req, symbols, sizing):
    """The per-symbol lots as submitted, so a stored run can be reloaded exactly.

    Lots, not the fraction: the fraction is derived and the form speaks lots, so
    storing only the fraction would make a reloaded run show a scale-out that was
    never typed.
    """
    submitted = {}
    if req.sizing:
        submitted = {e.symbol: e for e in req.sizing}
    out = {}
    for symbol in symbols:
        entry = submitted.get(symbol)
        out[symbol] = {
            "lot_size": entry.lot_size if entry else req.lot_size,
            "scale_out_lots": entry.scale_out_lots if entry else req.scale_out_lots,
            "partial_fraction": (sizing.get(symbol) or {}).get("partial_fraction"),
        }
    return out


@app.get("/backtests")
def list_backtests(symbol: Optional[str] = None, limit: int = 25):
    """Past runs, newest first, with the inputs that produced them."""
    try:
        return {"runs": repo.list_backtests(symbol, min(max(limit, 1), 200))}
    except DatabaseUnavailable as exc:
        return {"error": str(exc), "runs": []}

@app.get("/backtests/{run_id}")
def get_backtest(run_id: int):
    try:
        run = repo.get_backtest(run_id)
        if run is None:
            return {"error": "No backtest run %d" % run_id}
        return run
    except DatabaseUnavailable as exc:
        return {"error": str(exc)}

@app.delete("/backtests/{run_id}")
def delete_backtest(run_id: int):
    try:
        return {"deleted": repo.delete_backtest(run_id)}
    except DatabaseUnavailable as exc:
        return {"error": str(exc)}

@app.get("/preferences")
def get_preferences():
    """The dashboard's own state. Replaces localStorage.

    Returned with `available`, so the UI can tell "no preferences saved yet"
    from "the store is down" -- and not overwrite a stored theme with its
    default because a single poll failed.
    """
    try:
        return {"available": True, "preferences": repo.get_preferences()}
    except DatabaseUnavailable as exc:
        return {"available": False, "preferences": {}, "error": str(exc)}

@app.post("/preferences")
def save_preferences(update: PreferencesUpdate):
    try:
        return {"available": True, "preferences": repo.save_preferences(update.data)}
    except (DatabaseUnavailable, ValueError) as exc:
        return {"available": False, "error": str(exc)}

@app.post("/preferences/reset")
def reset_preferences():
    try:
        return {"available": True, "preferences": repo.replace_preferences({})}
    except DatabaseUnavailable as exc:
        return {"available": False, "error": str(exc)}

if __name__ == "__main__":
    if HOST != "127.0.0.1":
        log("WARNING: binding %s exposes UNAUTHENTICATED live-trading control" % HOST)
    uvicorn.run(app, host=HOST, port=PORT)
