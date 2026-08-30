import pip

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
MAGIC_NUMBER = 123456
TIMEFRAME = mt5.TIMEFRAME_M5

# Symbol configurations
# pip: price movement per pip (for SL/TP calculation)
# lot_size: position size
# sl_pips/tp_pips: stop loss and take profit in pips
# profit_mult: multiplier for P&L calculation (profit = price_diff * lot_size * profit_mult)
SYMBOL_CONFIG = {
    "XAUUSDm": {
        "pip": 0.1,
        "lot_size": 0.05,
        "sl_pips": 70,
        "tp_pips": 100,
        "profit_mult": 100,
    },
    "BTCUSDm": {
        "pip": 0.1,
        "lot_size": 0.05,
        "sl_pips": 700,
        "tp_pips": 500,
        "profit_mult": 1,
    },
}

SUPPORTED_SYMBOLS = list(SYMBOL_CONFIG.keys())

class TradingBot(threading.Thread):
    def __init__(self, symbol: str):
        super().__init__()
        self.symbol = symbol
        self.config = SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG["XAUUSDm"])
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
        
        # Vectorized calculation for the entire series
        outs = np.full(len(src), np.nan)
        for j in range(WINDOW_SIZE - 1, len(src)):
            window = src[j - WINDOW_SIZE + 1 : j + 1]
            val = np.sum(window * weights[::-1]) / sum_weights
            outs[j] = val
        
        # Calculate MAE for the entire series
        diffs = np.full(len(src), np.nan)
        for j in range(WINDOW_SIZE - 1, len(src)):
            window = src[j - WINDOW_SIZE + 1 : j + 1]
            val = np.sum(window * weights[::-1]) / sum_weights
            diffs[j] = abs(src[j] - val)
            
        # Use a rolling mean for MAE
        mae = pd.Series(diffs).rolling(window=WINDOW_SIZE).mean().values * MULT
        
        return outs, outs + mae, outs - mae

    def open_trade(self, action):
        tick = mt5.symbol_info_tick(self.symbol)
        symbol_info = mt5.symbol_info(self.symbol)
        price = tick.ask if action == "BUY" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
        lot_size = self.config["lot_size"]
        
        pip = self.config["pip"]
        if action == "BUY":
            sl = price - (self.config["sl_pips"] * pip)
            tp = price + (self.config["tp_pips"] * pip)
        else:
            sl = price + (self.config["sl_pips"] * pip)
            tp = price - (self.config["tp_pips"] * pip)
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lot_size,
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
                out_arr, upper_arr, lower_arr = self.calculate_envelope(df)
                out = out_arr[-1]
                upper = upper_arr[-1]
                lower = lower_arr[-1]
                
                self.stats.update({
                    "last_close": current_close,
                    "out": out_arr,
                    "upper": upper_arr,
                    "lower": lower_arr
                })
                
                # Update profit stats periodically
                self.update_performance_stats()
                
                positions = mt5.positions_get(symbol=self.symbol)
                if positions is None or len(positions) == 0:
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
        if symbol.upper() not in [s.upper() for s in SUPPORTED_SYMBOLS]:
            print(f"Bot only supports: {', '.join(SUPPORTED_SYMBOLS)}. Received: {symbol}")
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
            stats = self.bots[symbol].stats.copy()
            # Convert numpy arrays to latest scalar values for JSON serialization
            for key in ["out", "upper", "lower"]:
                if isinstance(stats[key], np.ndarray):
                    stats[key] = float(stats[key][-1]) if len(stats[key]) > 0 else 0.0
            return stats
        return {"status": "Stopped"}

    def run_backtest(self, symbol: str, start_date: datetime, end_date: datetime, initial_balance: float):
        if not mt5.initialize():
            return {"error": "MT5 Init Failed"}

        # Fetch rates
        rates = mt5.copy_rates_range(symbol, TIMEFRAME, start_date, end_date)
        if rates is None or len(rates) == 0:
            return {"error": "No historical data found for the given range"}

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')

        # Create a temporary bot instance to use its envelope logic
        bot = TradingBot(symbol)
        config = SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG["XAUUSDm"])
        outs, uppers, lowers = bot.calculate_envelope(df)

        balance = initial_balance
        equity = initial_balance
        trades_opened = 0
        wins = 0
        losses = 0
        total_pl = 0.0
        max_drawdown = 0.0
        peak_balance = initial_balance
        
        position = None # None, 'BUY', or 'SELL'
        entry_price = 0.0
        
        pip = config["pip"]
        sl_pips = config["sl_pips"] * pip
        tp_pips = config["tp_pips"] * pip
        lot_size = config["lot_size"]
        profit_mult = config["profit_mult"]

        for i in range(len(df)):
            price = df['close'].iloc[i]
            out = outs[i]
            upper = uppers[i]
            lower = lowers[i]
            
            if np.isnan(out) or np.isnan(upper) or np.isnan(lower):
                continue

            if position is None:
                if price < lower:
                    position = 'BUY'
                    entry_price = price
                    trades_opened += 1
                elif price > upper:
                    position = 'SELL'
                    entry_price = price
                    trades_opened += 1
            elif position == 'BUY':
                # Check TP/SL or Out
                if price >= entry_price + tp_pips or price <= entry_price - sl_pips or price >= out:
                    # Use actual exit price for TP/SL, or current price for 'out' exit
                    exit_price = price
                    if price <= entry_price - sl_pips:
                        exit_price = entry_price - sl_pips
                    elif price >= entry_price + tp_pips:
                        exit_price = entry_price + tp_pips
                        
                    pl = (exit_price - entry_price) * lot_size * profit_mult
                    balance += pl
                    total_pl += pl
                    if pl > 0: wins += 1
                    elif pl < 0: losses += 1
                    position = None
            elif position == 'SELL':
                # Check TP/SL or Out
                if price <= entry_price - tp_pips or price >= entry_price + sl_pips or price <= out:
                    # Use actual exit price for TP/SL, or current price for 'out' exit
                    exit_price = price
                    if price >= entry_price + sl_pips:
                        exit_price = entry_price + sl_pips
                    elif price <= entry_price - tp_pips:
                        exit_price = entry_price - tp_pips
                        
                    pl = (entry_price - exit_price) * lot_size * profit_mult
                    balance += pl
                    total_pl += pl
                    if pl > 0: wins += 1
                    elif pl < 0: losses += 1
                    position = None
            
            peak_balance = max(peak_balance, balance)
            drawdown = (peak_balance - balance) / peak_balance * 100 if peak_balance != 0 else 0
            max_drawdown = max(max_drawdown, drawdown)

        return {
            "initial_balance": initial_balance,
            "final_balance": balance,
            "total_pl": total_pl,
            "trades_opened": trades_opened,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / trades_opened * 100) if trades_opened > 0 else 0,
            "max_drawdown": max_drawdown
        }

    def get_account_info(self):
        acc = mt5.account_info()
        if acc is None: return {}
        
        # Convert named tuple to dict
        acc_dict = acc._asdict() if hasattr(acc, '_asdict') else dict(acc)
        
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
            "balance": acc_dict.get("balance", 0),
            "equity": acc_dict.get("equity", 0),
            "profit": acc_dict.get("profit", 0),
            "leverage": acc_dict.get("leverage", 0),
            "margin": acc_dict.get("margin", 0),
            "drawdown": ((acc_dict.get("balance", 0) - acc_dict.get("equity", 0)) / acc_dict.get("balance", 1) * 100) if acc_dict.get("balance", 0) != 0 else 0,
            "time_profits": profits
        }
