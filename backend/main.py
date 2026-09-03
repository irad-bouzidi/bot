import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from backend.bot_manager import (
    BotManager, SUPPORTED_SYMBOLS, log, scale_out_fraction,
)
from backend.core.errors import ConfigRejected
import uvicorn
from datetime import datetime

# S2: this API can start and stop LIVE trading and has no authentication, so it
# must not be reachable from the network. Bind loopback and allow only the local
# dev frontend. Override deliberately via env if you know what you are doing.
HOST = os.environ.get("BOT_HOST", "127.0.0.1")
PORT = int(os.environ.get("BOT_PORT", "8000"))
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
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

manager = BotManager()


@app.on_event("shutdown")
def _shutdown():
    """S6: stop bots on exit. Previously the non-daemon threads kept a live
    position open and Ctrl+C on uvicorn would hang."""
    log("Shutting down: stopping all bots")
    manager.stop_all()

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

@app.get("/stats")
def get_stats():
    return {
        "account": manager.get_account_info(),
        "bots": {
            symbol: manager.get_bot_stats(symbol)
            for symbol in SUPPORTED_SYMBOLS
        }
    }

@app.post("/control")
def control_bot(ctrl: SymbolControl):
    if ctrl.action == "start":
        manager.start_bot(ctrl.symbol)
        return {"message": f"Bot started for {ctrl.symbol}"}
    elif ctrl.action == "stop":
        manager.stop_bot(ctrl.symbol)
        return {"message": f"Bot stopped for {ctrl.symbol}"}
    return {"error": "Invalid action"}

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

@app.post("/backtest")
def backtest(req: BacktestRequest):
    try:
        start = datetime.fromisoformat(req.start_date)
        end = datetime.fromisoformat(req.end_date)
        fraction = None
        if req.scale_out_lots is not None:
            # Against the lot size THIS RUN uses, not the live one: the pair has to
            # be interpreted together or the fraction means something else.
            lot = (req.lot_size if req.lot_size is not None
                   else manager.get_settings(req.symbol)["lot_size"])
            fraction = scale_out_fraction(lot, req.scale_out_lots)
        result = manager.run_backtest(req.symbol, start, end, req.initial_balance,
                                      lot_size=req.lot_size,
                                      partial_fraction=fraction)
        return result
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    if HOST != "127.0.0.1":
        log("WARNING: binding %s exposes UNAUTHENTICATED live-trading control" % HOST)
    uvicorn.run(app, host=HOST, port=PORT)
