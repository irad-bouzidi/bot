import os
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any, Dict, Optional
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

# The containerised frontend is served by nginx on :3000 and the BROWSER still
# calls this API on 127.0.0.1:8000 directly -- nginx only serves static files
# and does NOT proxy to the backend. That is deliberate: proxying would need
# this process reachable from the Docker bridge network, i.e. bound off
# loopback, which would put unauthenticated live-trading control on the host's
# network interface. Adding the container's published origin to CORS costs
# nothing by comparison. See docker/README.md.
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

class BacktestRequest(BaseModel):
    symbol: str
    start_date: str
    end_date: str
    initial_balance: float
    # Override the live sizing for this run only. Omitted -> whatever the bot is
    # currently configured with, so the default backtest matches the default bot.
    lot_size: Optional[float] = None
    scale_out_lots: Optional[float] = None

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
    """Run the legacy engine and STORE the run, inputs and outputs together.

    Errored runs are stored too. A run that found no bars for its window is a
    fact about that window, and dropping it is how the same unavailable range
    gets asked for five times.
    """
    started = time.time()
    fraction = None
    start = end = None
    try:
        start = datetime.fromisoformat(req.start_date)
        end = datetime.fromisoformat(req.end_date)
        if req.scale_out_lots is not None:
            # Against the lot size THIS RUN uses, not the live one: the pair has to
            # be interpreted together or the fraction means something else.
            lot = (req.lot_size if req.lot_size is not None
                   else manager.get_settings(req.symbol)["lot_size"])
            fraction = scale_out_fraction(lot, req.scale_out_lots)
        result = manager.run_backtest(req.symbol, start, end, req.initial_balance,
                                      lot_size=req.lot_size,
                                      partial_fraction=fraction)
    except Exception as e:
        _store_backtest(req, start, end, fraction, None, str(e), started)
        return {"error": str(e)}

    # run_backtest reports "no bars in range" as an {"error": ...} body rather
    # than by raising, so the stored status has to come from the payload.
    error = result.get("error") if isinstance(result, dict) else None
    stored = _store_backtest(req, start, end, fraction, result, error, started)
    if error:
        return result
    if stored is not None:
        result = dict(result)
        result["run_id"] = stored["id"]
        result["created_at"] = stored["created_at"]
    return result


def _store_backtest(req, start, end, fraction, result, error, started):
    """Record one run. Never raises -- a storage failure must not lose the run's
    RESULT, which the user is waiting on and which took real time to compute."""
    try:
        return repo.record_backtest(
            symbol=req.symbol,
            start_date=start or req.start_date,
            end_date=end or req.end_date,
            initial_balance=req.initial_balance,
            lot_size=req.lot_size,
            scale_out_lots=req.scale_out_lots,
            partial_fraction=fraction,
            engine="legacy",
            status="error" if error else "ok",
            error=error,
            result=None if error else result,
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as exc:
        log("db: could not store the backtest run -- %r" % exc)
        return None

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
