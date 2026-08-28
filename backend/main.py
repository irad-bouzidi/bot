from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from backend.bot_manager import BotManager
import uvicorn
from datetime import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = BotManager()

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
            "XAUUSDm": manager.get_bot_stats("XAUUSDm")
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
    uvicorn.run(app, host="0.0.0.0", port=8000)
