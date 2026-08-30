import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
import threading
from datetime import datetime, timedelta
from typing import Dict

from backend.indicators.nadaraya_watson import nw_envelope, nw_warmup_bars

# Constants
BANDWIDTH = 8.0
MULT = 3.0
WINDOW_SIZE = 500
MAGIC_NUMBER = 123456
TIMEFRAME = mt5.TIMEFRAME_M5

# Seconds per bar for TIMEFRAME (M5). Used for the cooldown between entries.
TIMEFRAME_SECONDS = 300

# The envelope needs (WINDOW_SIZE - 1) bars for `out` plus WINDOW_SIZE bars for the
# rolling MAE, so the first usable index is 2*WINDOW_SIZE - 2 (= 998). Fetching
# exactly WINDOW_SIZE*2 left only 2 usable bars, and dropping the forming bar left
# 1. Anything short of that yields all-NaN bands, which compare False against every
# price -- the bot would silently never trade while reporting "Running".
# MAE_WINDOW 500 reproduces the original rolling(500). Pine uses 499
# (`ta.sma(abs(src-out), 499)`); that off-by-one is now explicit rather than accidental.
MAE_WINDOW = 500
WARMUP_BARS = nw_warmup_bars(window=WINDOW_SIZE, mae_window=MAE_WINDOW)  # 998
MIN_USABLE_BARS = WARMUP_BARS + 1                                        # 999
FETCH_BARS = MIN_USABLE_BARS + 201                                       # + forming bar + margin

# Minimum closed bars between two entries, to stop the bot re-firing the same signal.
COOLDOWN_BARS = 3

# Max price slippage tolerated on a market order, in points.
DEVIATION_POINTS = 20

# symbol_info().filling_mode is a BITMASK and does not share values with the
# mt5.ORDER_FILLING_* order constants. Keep them separate.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)

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
        self.daemon = True  # S6: never block interpreter shutdown
        self.symbol = symbol
        self.config = SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG["XAUUSDm"])
        self.running = False
        self._stop_event = threading.Event()
        self._last_bar_time = None    # S4: last CLOSED bar already acted on
        self._last_entry_bar = None   # S4: bar time of the most recent entry
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
        """Delegates to the shared, vectorised indicator.

        The previous body ran the identical O(n*500) Python loop twice -- once for
        `out` and once for `|src - out|`, recomputing the same value. The shared
        version is a single np.convolve, ~54x faster in total and verified equal to
        the original loop to 9.1e-13 (tests/test_indicators_nw.py).
        """
        env = nw_envelope(
            df["close"].values,
            bandwidth=BANDWIDTH,
            mult=MULT,
            window=WINDOW_SIZE,
            mae_window=MAE_WINDOW,
        )
        return env.out, env.upper, env.lower

    # ---- Phase 0 execution helpers -------------------------------------------

    def _symbol_info(self):
        """symbol_info(), selecting the symbol into Market Watch if needed."""
        info = mt5.symbol_info(self.symbol)
        if info is None:
            return None
        if not info.visible:
            if not mt5.symbol_select(self.symbol, True):
                log("%s: symbol_select failed: %s" % (self.symbol, mt5.last_error()))
                return None
            info = mt5.symbol_info(self.symbol)
        return info

    @staticmethod
    def _pick_filling(info):
        """S3: derive the filling mode from the symbol instead of hardcoding IOC.

        symbol_info().filling_mode is a bitmask (FOK=1, IOC=2) whose values are NOT
        the mt5.ORDER_FILLING_* constants. Hardcoding IOC makes every order fail
        with retcode 10030 on brokers that only allow FOK.
        """
        modes = getattr(info, "filling_mode", 0) or 0
        if modes & SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        if modes & SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    @staticmethod
    def _round_price(price, info):
        """S5: snap to the symbol's tick size, then to its digit count."""
        step = getattr(info, "trade_tick_size", 0) or info.point
        if step and step > 0:
            price = round(price / step) * step
        return round(price, info.digits)

    def _apply_stops(self, action, price, sl, tp, info):
        """S5: honour the broker's minimum stop distance, then normalize.

        SL/TP were previously sent raw, so an un-rounded price or a stop closer
        than trade_stops_level was rejected with 10015/10016 and never logged.
        Stops are only ever widened here, never tightened.
        """
        min_dist = (getattr(info, "trade_stops_level", 0) or 0) * info.point
        if min_dist > 0:
            if action == "BUY":
                sl = min(sl, price - min_dist)
                tp = max(tp, price + min_dist)
            else:
                sl = max(sl, price + min_dist)
                tp = min(tp, price - min_dist)
        return self._round_price(sl, info), self._round_price(tp, info)

    def _send(self, request, what):
        """S3: order_send returns None on transport failure -- guard and log it.

        Previously `result.retcode` raised AttributeError on None, which the loop's
        blanket `except Exception` swallowed. Failed orders were entirely invisible.
        """
        result = mt5.order_send(request)
        if result is None:
            log("%s: %s order_send returned None, last_error=%s"
                % (self.symbol, what, mt5.last_error()))
            return None
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log("%s: %s rejected retcode=%s comment=%s last_error=%s"
                % (self.symbol, what, result.retcode, result.comment, mt5.last_error()))
            return None
        return result

    def bot_positions(self):
        """S1: ONLY this bot's positions.

        positions_get(symbol=...) returns every position on the symbol regardless of
        origin. Unfiltered, the bot closed the user's MANUAL trades, and a single
        manual position blocked it from ever entering.
        """
        positions = mt5.positions_get(symbol=self.symbol)
        if positions is None:
            return []
        return [p for p in positions if p.magic == MAGIC_NUMBER]

    # ---- orders ---------------------------------------------------------------

    def open_trade(self, action):
        info = self._symbol_info()
        tick = mt5.symbol_info_tick(self.symbol)
        if info is None or tick is None:
            log("%s: no symbol info/tick, skipping entry" % self.symbol)
            return False

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

        sl, tp = self._apply_stops(action, price, sl, tp, info)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lot_size,
            "type": order_type,
            "price": self._round_price(price, info),
            "sl": sl,
            "tp": tp,
            "deviation": DEVIATION_POINTS,
            "magic": MAGIC_NUMBER,
            "comment": "NW Dashboard Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._pick_filling(info),
        }
        if self._send(request, "%s entry" % action) is None:
            return False
        self.stats["trades_opened"] += 1
        log("%s: opened %s @ %.5f sl=%.5f tp=%.5f" % (self.symbol, action, price, sl, tp))
        return True

    def close_position(self, position):
        info = self._symbol_info()
        tick = mt5.symbol_info_tick(position.symbol)
        if info is None or tick is None:
            log("%s: no symbol info/tick, skipping close" % self.symbol)
            return False

        is_buy = position.type == mt5.POSITION_TYPE_BUY
        price = tick.bid if is_buy else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": self._round_price(price, info),
            "deviation": DEVIATION_POINTS,
            "magic": MAGIC_NUMBER,
            "comment": "Closing NW Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._pick_filling(info),
        }
        if self._send(request, "close #%s" % position.ticket) is None:
            return False
        log("%s: closed #%s @ %.5f" % (self.symbol, position.ticket, price))
        return True

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

    def _sleep(self, seconds):
        """Responsive sleep: wakes immediately on stop()."""
        for _ in range(int(seconds)):
            if self._stop_event.is_set():
                return
            time.sleep(1)

    def run(self):
        self.running = True
        self.stats["status"] = "Running"
        last_stats_refresh = 0.0

        while not self._stop_event.is_set():
            try:
                rates = mt5.copy_rates_from_pos(self.symbol, TIMEFRAME, 0, FETCH_BARS)
                if rates is None or len(rates) == 0:
                    log("%s: no rates (%s); MT5 may be disconnected"
                        % (self.symbol, mt5.last_error()))
                    self._sleep(10)
                    continue

                df = pd.DataFrame(rates)

                # S4: index -1 is the bar STILL FORMING. Acting on it meant the
                # signal was re-evaluated ~5x per M5 candle and could fire on a wick
                # that the bar then reverted -- and it made live disagree with the
                # backtest, which uses closed bars. Drop it.
                df = df.iloc[:-1].reset_index(drop=True)

                if len(df) < MIN_USABLE_BARS:
                    log("%s: only %d closed bars, need %d for a valid envelope; "
                        "not trading" % (self.symbol, len(df), MIN_USABLE_BARS))
                    self._sleep(30)
                    continue

                current_close = df["close"].iloc[-1]
                bar_time = int(df["time"].iloc[-1])
                out_arr, upper_arr, lower_arr = self.calculate_envelope(df)
                out = out_arr[-1]
                upper = upper_arr[-1]
                lower = lower_arr[-1]

                self.stats.update({
                    "last_close": current_close,
                    "out": out_arr,
                    "upper": upper_arr,
                    "lower": lower_arr,
                })

                # Refresh P&L at most once a minute. This is a 365-day history scan
                # over IPC; it does not belong in the signal path on every tick.
                now = time.time()
                if now - last_stats_refresh >= 60:
                    self.update_performance_stats()
                    last_stats_refresh = now

                if np.isnan(out) or np.isnan(upper) or np.isnan(lower):
                    log("%s: envelope is NaN, skipping this bar" % self.symbol)
                    self._sleep(15)
                    continue

                # S4: act at most once per closed bar.
                if bar_time == self._last_bar_time:
                    self._sleep(15)
                    continue

                positions = self.bot_positions()   # S1: this bot's positions only

                if not positions:
                    cooled = (
                        self._last_entry_bar is None
                        or (bar_time - self._last_entry_bar) >= COOLDOWN_BARS * TIMEFRAME_SECONDS
                    )
                    if not cooled:
                        pass  # still inside the post-entry cooldown
                    elif current_close < lower:
                        if self.open_trade("BUY"):
                            self._last_entry_bar = bar_time
                    elif current_close > upper:
                        if self.open_trade("SELL"):
                            self._last_entry_bar = bar_time
                else:
                    for pos in positions:
                        if pos.type == mt5.POSITION_TYPE_BUY and current_close >= out:
                            self.close_position(pos)
                        elif pos.type == mt5.POSITION_TYPE_SELL and current_close <= out:
                            self.close_position(pos)

                self._last_bar_time = bar_time
                self._sleep(15)

            except Exception as e:
                log("Bot Error (%s): %r" % (self.symbol, e))
                self._sleep(10)

        self.stats["status"] = "Stopped"
        self.running = False


class BotManager:
    def __init__(self):
        self.bots: Dict[str, TradingBot] = {}
        if not mt5.initialize():
            log("MT5 Init Failed: %s" % (mt5.last_error(),))

    def start_bot(self, symbol: str):
        if symbol.upper() not in [s.upper() for s in SUPPORTED_SYMBOLS]:
            log("Bot only supports: %s. Received: %s" % (", ".join(SUPPORTED_SYMBOLS), symbol))
            return

        if symbol in self.bots and self.bots[symbol].is_alive():
            return

        old_stats = dict(self.bots[symbol].stats) if symbol in self.bots else None
        self.bots[symbol] = TradingBot(symbol)
        if old_stats:
            old_stats.pop("status", None)  # never resurrect a stale status
            self.bots[symbol].stats.update(old_stats)
        
        self.bots[symbol].start()
        
    def stop_bot(self, symbol: str):
        if symbol in self.bots:
            self.bots[symbol].stop()

    def stop_all(self):
        """S6: stop every bot and join briefly, so shutdown cannot hang."""
        for symbol, bot in list(self.bots.items()):
            bot.stop()
        for symbol, bot in list(self.bots.items()):
            if bot.is_alive():
                bot.join(timeout=5)
                if bot.is_alive():
                    log("%s: bot thread did not stop within 5s" % symbol)
        mt5.shutdown()

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
