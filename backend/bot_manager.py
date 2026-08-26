import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
import threading
from datetime import datetime, timedelta
from typing import Dict

# Constants
BANDWIDTH = 8.0
MULT = 3.0
WINDOW_SIZE = 500
LOT_SIZE = 0.10
MAGIC_NUMBER = 123456
TIMEFRAME = mt5.TIMEFRAME_M5

class TradingBot(threading.Thread):
    def __init__(self, symbol: str):
        super().__init__()
        self.symbol = symbol
        self.running = False
        self._stop_event = threading.Event()
        self.stats = {
            "last_close": 0.0,
            "out": 0.0,
            "upper": 0.0,
            "lower": 0.0,
            "status": "Stopped",
            "trades_opened": 0,
            "wins": 0,
            "losses": 0,
            "total_pl": 0.0,
            "max_drawdown": 0.0
        }

    def stop(self):
        self.running = False
        self._stop_event.set()

    def calculate_envelope(self, df):
        close = df['close'].values
        src = close
        h = BANDWIDTH
        i_vals = np.arange(WINDOW_SIZE)
        weights = np.exp(-(i_vals**2 / (2 * h**2)))
        sum_weights = np.sum(weights)
        
        outs = []
        for j in range(len(src) - WINDOW_SIZE, len(src)):
            window = src[j - WINDOW_SIZE + 1 : j + 1]
            if len(window) < WINDOW_SIZE:
                outs.append(np.nan)
                continue
            val = np.sum(window * weights[::-1]) / sum_weights
            outs.append(val)
        
        current_out = outs[-1]
        
        diffs = []
        for j in range(len(src) - WINDOW_SIZE * 2, len(src)):
            window = src[j - WINDOW_SIZE + 1 : j + 1]
            if len(window) < WINDOW_SIZE:
                diffs.append(np.nan)
                continue
            val = np.sum(window * weights[::-1]) / sum_weights
            diffs.append(abs(src[j] - val))
            
        mae = np.nanmean(diffs[-WINDOW_SIZE:]) * MULT
        return current_out, current_out + mae, current_out - mae

    def open_trade(self, action):
        tick = mt5.symbol_info_tick(self.symbol)
        symbol_info = mt5.symbol_info(self.symbol)
        
        price = tick.ask if action == "BUY" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
        
        # For Gold: 1 USD = 10 pips, so 1 pip = 0.1 USD
        pip = 0.1
        
        if action == "BUY":
            sl = price - (70 * pip)
            tp = price + (50 * pip)
        else:
            sl = price + (70 * pip)
            tp = price - (50 * pip)
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": LOT_SIZE,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "magic": MAGIC_NUMBER,
            "comment": "NW Dashboard Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self.stats["trades_opened"] += 1
            return True
        return False

    def close_position(self, position):
        tick = mt5.symbol_info_tick(position.symbol)
        price = tick.bid if position.type == mt5.POSITION_TYPE_BUY else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": price,
            "magic": MAGIC_NUMBER,
            "comment": "Closing NW Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            # We'll update trade stats in a separate method via history
            pass

    def update_performance_stats(self):
        """Calculates P&L, Wins, Losses from MT5 History."""
        # Get history for the last 365 days
        from_date = datetime.now() - timedelta(days=365)
        history = mt5.history_deals_get(from_date, datetime.now(), group=f"*{self.symbol}*")
        
        if history is None or len(history) == 0:
            return

        # Filter by magic number to only count this bot's trades
        bot_deals = [d for d in history if d.magic == MAGIC_NUMBER]
        
        total_pl = 0.0
        wins = 0
        losses = 0
        
        for deal in bot_deals:
            # We only care about the closing deals (entry deals have 0 profit usually)
            if deal.entry == mt5.DEAL_ENTRY_OUT:
                profit = deal.profit
                total_pl += profit
                if profit > 0:
                    wins += 1
                elif profit < 0:
                    losses += 1

        self.stats["total_pl"] = total_pl
        self.stats["wins"] = wins
        self.stats["losses"] = losses

    def run(self):
        self.running = True
        self.stats["status"] = "Running"
        
        while not self._stop_event.is_set():
            try:
                rates = mt5.copy_rates_from_pos(self.symbol, TIMEFRAME, 0, WINDOW_SIZE * 2)
                if rates is None:
                    # Responsive sleep
                    for _ in range(10): 
                        if self._stop_event.is_set(): break
                        time.sleep(1)
                    continue
                
                df = pd.DataFrame(rates)
                current_close = df['close'].iloc[-1]
                out, upper, lower = self.calculate_envelope(df)
                
                self.stats.update({
                    "last_close": current_close,
                    "out": out,
                    "upper": upper,
                    "lower": lower
                })
                
                # Update profit stats periodically
                self.update_performance_stats()
                
                positions = mt5.positions_get(symbol=self.symbol)
                if not positions:
                    if current_close < lower:
                        self.open_trade("BUY")
                    elif current_close > upper:
                        self.open_trade("SELL")
                else:
                    for pos in positions:
                        if pos.type == mt5.POSITION_TYPE_BUY and current_close >= out:
                            self.close_position(pos)
                        elif pos.type == mt5.POSITION_TYPE_SELL and current_close <= out:
                            self.close_position(pos)
                
                # Responsive sleep for 60 seconds
                for _ in range(60):
                    if self._stop_event.is_set(): break
                    time.sleep(1)
                    
            except Exception as e:
                print(f"Bot Error ({self.symbol}): {e}")
                for _ in range(10):
                    if self._stop_event.is_set(): break
                    time.sleep(1)
        
        self.stats["status"] = "Stopped"
        self.running = False

class BotManager:
    def __init__(self):
        self.bots: Dict[str, TradingBot] = {}
        if not mt5.initialize():
            print("MT5 Init Failed")

    def start_bot(self, symbol: str):
        if symbol.upper() not in ["XAUUSD", "GOLD", "XAUUSDM"]:
            print(f"Bot only supports Gold (XAUUSD/GOLD/XAUUSDm). Received: {symbol}")
            return

        if symbol in self.bots and self.bots[symbol].is_alive():
            return

        old_stats = self.bots[symbol].stats if symbol in self.bots else None
        self.bots[symbol] = TradingBot(symbol)
        if old_stats:
            self.bots[symbol].stats.update(old_stats)
        
        self.bots[symbol].start()
        
    def stop_bot(self, symbol: str):
        if symbol in self.bots:
            self.bots[symbol].stop()

    def get_bot_stats(self, symbol: str):
        if symbol in self.bots:
            return self.bots[symbol].stats
        return {"status": "Stopped"}

    def get_account_info(self):
        acc = mt5.account_info()
        if acc is None: return {}
        
        # Calculate time-based profits
        now = datetime.now()
        time_frames = {
            "daily": now - timedelta(days=1),
            "weekly": now - timedelta(weeks=1),
            "monthly": now - timedelta(days=30),
            "yearly": now - timedelta(days=365),
        }
        
        profits = {}
        for label, start_date in time_frames.items():
            history = mt5.history_deals_get(start_date, now)
            if history:
                profits[label] = sum(d.profit for d in history if d.entry == mt5.DEAL_ENTRY_OUT)
            else:
                profits[label] = 0.0

        return {
            "balance": acc.balance,
            "equity": acc.equity,
            "profit": acc.profit,
            "leverage": acc.leverage,
            "margin": acc.margin,
            "drawdown": ((acc.balance - acc.equity) / acc.balance * 100) if acc.balance != 0 else 0,
            "time_profits": profits
        }
