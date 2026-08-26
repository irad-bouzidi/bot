from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from backend.bot_manager import BotManager
import uvicorn

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

@app.get("/stats")
def get_stats():
    return {
        "account": manager.get_account_info(),
        "bots": {
            "XAUUSDm": manager.get_bot_stats("XAUUSDm"),
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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
