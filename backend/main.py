import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from backend.bot_manager import BotManager, SUPPORTED_SYMBOLS, log
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

class BacktestRequest(BaseModel):
    symbol: str
    start_date: str
    end_date: str
    initial_balance: float

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

@app.post("/backtest")
def backtest(req: BacktestRequest):
    try:
        start = datetime.fromisoformat(req.start_date)
        end = datetime.fromisoformat(req.end_date)
        result = manager.run_backtest(req.symbol, start, end, req.initial_balance)
        return result
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    if HOST != "127.0.0.1":
        log("WARNING: binding %s exposes UNAUTHENTICATED live-trading control" % HOST)
    uvicorn.run(app, host=HOST, port=PORT)
