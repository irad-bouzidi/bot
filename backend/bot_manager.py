import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import json
import math
import os
import time
import threading
from datetime import datetime, timedelta
from typing import Dict

from backend.core.errors import ConfigRejected
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
# lot_size: position size. EDITABLE at runtime -- see EDITABLE_KEYS below.
# sl_pips/tp_pips: stop loss and take profit in pips
# profit_mult: multiplier for P&L calculation (profit = price_diff * lot_size * profit_mult)
# be_trigger_pips: profit distance at which the scale-out arms. Half the target --
#   see NWConfig.be_trigger_mode="tp_fraction", the same rule.
# partial_fraction: proportion of the position closed at that trigger. 0.5 of the
#   0.1 default is 0.05 out and 0.05 left running to the target, which is what was
#   asked for. It is a FRACTION, not a lot count, so it tracks lot_size instead of
#   silently becoming a different share of the position when the size changes.
#   EDITABLE at runtime, but only ever via scale_out_fraction(): the UI speaks
#   lots, this dict does not.
SYMBOL_CONFIG = {
    "XAUUSDm": {
        "pip": 0.1,
        "lot_size": 0.1,            # risks ~$70/trade at the 70-pip stop
        "sl_pips": 70,
        "tp_pips": 100,
        "profit_mult": 100,
        "be_trigger_pips": 50,      # 5.00 in price -- half of the 100-pip target
        "partial_fraction": 0.5,    # 0.05 out at +5.00, 0.05 runs to the target
    },
}

SUPPORTED_SYMBOLS = list(SYMBOL_CONFIG.keys())

# ---- runtime-editable sizing -------------------------------------------------
#
# The dashboard can edit exactly these two keys; every other key above is fixed in
# code. They are persisted, because with no equity-based sizing anywhere in this
# bot the lot size IS the risk control: someone who lowers it to 0.02 to cut their
# exposure must not have 0.1 -- and ~$70 a trade -- quietly restored by a restart.
EDITABLE_KEYS = ("lot_size", "partial_fraction")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_FILE = os.environ.get(
    "BOT_SETTINGS_FILE", os.path.join(_REPO_ROOT, "data", "settings.json"))

# Guards every read-modify-write of SYMBOL_CONFIG. open_trade() holds it across the
# order_send and update_settings() holds it across "is this bot flat?" -> "write",
# so a size edit cannot land between the two and leave manage_position() comparing
# a live position against a lot_size it was never opened with.
_CONFIG_LOCK = threading.RLock()


def scale_out_fraction(lot_size, scale_out_lots):
    """Convert the UI's lot count into the stored FRACTION. The only conversion.

    The rule is a proportion of the position, never a lot count: 0.05 out of 0.1 is
    "half", and storing the 0.05 itself would silently become a quarter the moment
    the lot size went to 0.2. Lots are the unit a trader thinks in, so the
    translation happens here at the one boundary, both numbers are always submitted
    together, and the resulting percentage is echoed straight back -- which is what
    keeps a re-scale visible instead of silent.
    """
    lot_size = float(lot_size)
    scale_out_lots = float(scale_out_lots)
    if not math.isfinite(lot_size) or lot_size <= 0:
        raise ConfigRejected("Lot size must be a positive number.")
    if not math.isfinite(scale_out_lots) or scale_out_lots < 0:
        raise ConfigRejected("Scale-out lots cannot be negative.")
    if scale_out_lots >= lot_size:
        raise ConfigRejected(
            "Scale-out %g must be smaller than the lot size %g -- closing the whole "
            "position at the trigger is a different rule. Use 0 to turn the "
            "scale-out off." % (scale_out_lots, lot_size))
    return scale_out_lots / lot_size


def _validated(key, value):
    """Range-check one EDITABLE_KEYS value, without needing a live terminal."""
    value = float(value)
    if not math.isfinite(value):
        raise ConfigRejected("%s must be a finite number" % key)
    if key == "lot_size" and value <= 0:
        raise ConfigRejected("lot_size must be positive")
    if key == "partial_fraction" and not 0.0 <= value < 1.0:
        raise ConfigRejected("partial_fraction must be in [0, 1)")
    return value


def _load_settings():
    """Apply persisted sizing over SYMBOL_CONFIG at import time.

    Deliberately narrow: only EDITABLE_KEYS, only for symbols already defined in
    code, and only values that survive validation. A file on disk must never be
    able to introduce a symbol or move a stop -- it can only change the two numbers
    the UI is allowed to change.
    """
    try:
        with open(SETTINGS_FILE) as fh:
            saved = json.load(fh)
    except (IOError, OSError):
        return
    except ValueError:
        log("settings: %s is not valid JSON; using the defaults" % SETTINGS_FILE)
        return
    if not isinstance(saved, dict):
        return
    for symbol, values in saved.items():
        cfg = SYMBOL_CONFIG.get(symbol)
        if cfg is None or not isinstance(values, dict):
            continue
        for key in EDITABLE_KEYS:
            if key not in values:
                continue
            try:
                cfg[key] = _validated(key, values[key])
            except (ConfigRejected, TypeError, ValueError) as exc:
                log("settings: ignoring %s.%s -- %s" % (symbol, key, exc))
        log("settings: %s loaded lot_size=%g partial_fraction=%g"
            % (symbol, cfg["lot_size"], cfg.get("partial_fraction", 0.0)))


def _save_settings():
    """Persist EDITABLE_KEYS. Call with _CONFIG_LOCK held."""
    payload = dict((symbol, dict((k, cfg[k]) for k in EDITABLE_KEYS if k in cfg))
                   for symbol, cfg in SYMBOL_CONFIG.items())
    folder = os.path.dirname(SETTINGS_FILE)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    # Write-then-rename: a crash partway through must not leave a truncated file,
    # because _load_settings() would then fall back to the 0.1 default and silently
    # undo a size that was lowered on purpose.
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, SETTINGS_FILE)


def _volume_limits(symbol):
    """(min, max, step, came_from_broker) for `symbol`.

    Falls back to conventional values when the terminal is down, so the settings
    form still validates and still snaps to a step; the broker re-checks the volume
    on the order itself either way.
    """
    try:
        info = mt5.symbol_info(symbol)
    except Exception:
        info = None
    if info is None:
        return 0.01, 100.0, 0.01, False
    return (float(getattr(info, "volume_min", 0.01) or 0.01),
            float(getattr(info, "volume_max", 100.0) or 100.0),
            float(getattr(info, "volume_step", 0.01) or 0.01),
            True)


def _snap_volume(volume, vol_min, vol_max, step):
    """Snap to the symbol's volume step and clamp to its limits."""
    step = step or 0.01
    vol = round(volume / step) * step
    return round(max(vol_min, min(vol_max, vol)), 8)


def _split_lots(lot_size, fraction, vol_min, step):
    """(scale_out_lots, runner_lots) -- what the broker can actually execute.

    The snap and the minimum-volume check are decided from the SETTINGS rather than
    from a live position, because lot_size is constant between trades. 0.01 lots
    cannot be halved at any broker; callers still arm break-even in that case,
    exactly as manage_position() does, instead of dropping the whole rule.
    """
    if fraction <= 0 or lot_size <= 0:
        return 0.0, round(lot_size, 8)
    step = step or 0.01
    want = round(round(lot_size * fraction / step) * step, 8)
    runner = round(lot_size - want, 8)
    if want <= 0 or runner < vol_min - 1e-9:
        return 0.0, round(lot_size, 8)
    return want, runner


def bot_positions(symbol):
    """S1: ONLY this bot's positions.

    positions_get(symbol=...) returns every position on the symbol regardless of
    origin. Unfiltered, the bot closed the user's MANUAL trades, and a single
    manual position blocked it from ever entering.
    """
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return []
    return [p for p in positions if p.magic == MAGIC_NUMBER]


_load_settings()

class TradingBot(threading.Thread):
    def __init__(self, symbol: str):
        super().__init__()
        self.daemon = True  # S6: never block interpreter shutdown
        self.symbol = symbol
        # A live REFERENCE into SYMBOL_CONFIG, not a copy: update_settings() edits
        # that dict in place so a running bot picks the new size up on its next
        # entry without being restarted. Read it under _CONFIG_LOCK where the value
        # has to stay consistent with a position (see open_trade).
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
        """S1: ONLY this bot's positions -- see the module-level function."""
        return bot_positions(self.symbol)

    # ---- orders ---------------------------------------------------------------

    def open_trade(self, action):
        info = self._symbol_info()
        tick = mt5.symbol_info_tick(self.symbol)
        if info is None or tick is None:
            log("%s: no symbol info/tick, skipping entry" % self.symbol)
            return False

        price = tick.ask if action == "BUY" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL

        # Held across the read of lot_size AND the order_send. update_settings()
        # takes the same lock to check "is this bot flat?" before writing, so
        # without it a size edit could slip between that check and this send, and
        # manage_position() would then measure a position against a lot_size it was
        # not opened with -- and scale an already-reduced position out twice.
        with _CONFIG_LOCK:
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

    # ---- scale-out / break-even ----------------------------------------------

    def _round_volume(self, volume, info):
        """Snap to the symbol's volume step and clamp to its limits."""
        return _snap_volume(volume,
                            getattr(info, "volume_min", 0.01) or 0.01,
                            getattr(info, "volume_max", 100.0) or 100.0,
                            getattr(info, "volume_step", 0.0) or 0.01)

    def _modify_sl(self, position, new_sl, info):
        """Move the stop on an OPEN position, keeping its take profit.

        TRADE_ACTION_SLTP replaces both fields, so tp must be passed back
        explicitly -- omitting it silently deletes the take profit.
        """
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": position.ticket,
            "sl": self._round_price(new_sl, info),
            "tp": position.tp,
            "magic": MAGIC_NUMBER,
        }
        return self._send(request, "move SL #%s" % position.ticket) is not None

    def _close_partial(self, position, volume, info):
        """Close `volume` lots of an open position, leaving the rest running.

        A partial close is an ordinary opposite DEAL carrying `position`, with a
        volume smaller than the position's. MT5 keeps the SAME ticket and reduces
        the position's volume, which is what makes the stateless "has it fired yet"
        check below work across a bot restart.
        """
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            return False
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        price = tick.bid if is_buy else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": self._round_price(price, info),
            "deviation": DEVIATION_POINTS,
            "magic": MAGIC_NUMBER,
            "comment": "NW partial TP",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._pick_filling(info),
        }
        if self._send(request, "partial close #%s" % position.ticket) is None:
            return False
        log("%s: scaled out %.2f of #%s @ %.5f (entry %.5f)"
            % (self.symbol, volume, position.ticket, price, position.price_open))
        return True

    def manage_position(self, position, info, tick):
        """At `be_trigger_pips` in profit: bank part of the position, stop to entry.

        Deliberately STATELESS -- it re-derives what still needs doing from the
        position itself rather than from a flag on the bot:

          * still to scale out  <=>  volume is still the full lot_size
          * still to move stop  <=>  sl is missing, or not yet at entry

        A dict of tickets would have to survive restarts, reconnects and the
        BotManager replacing the thread on start_bot(), and would leak a ticket per
        trade. The position is the single source of truth, so ask it.

        The two guards are independent on purpose: if the scale-out fills but the
        stop move is rejected, the next cycle retries only the stop, and vice versa.

        That first guard is why `lot_size` cannot be edited while a position is
        open: shrinking it mid-trade would make an already-reduced position look
        un-scaled and scale it out a second time. Now that the dashboard can edit
        the size, this is no longer an assumption -- BotManager.update_settings()
        refuses the edit while this bot holds a position, under the same
        _CONFIG_LOCK that open_trade() holds across its order_send.

        Runs on the live tick, not on bar close: the trigger is an intrabar event,
        and waiting for a close would skip every move that gave the profit back
        before the candle ended. Polling is ~15s, so a spike that round-trips
        inside one cycle is still missed -- by design, since the bot only ever acts
        on profit it can realise at the price in front of it.
        """
        with _CONFIG_LOCK:
            trigger_pips = self.config.get("be_trigger_pips", 0)
            fraction = self.config.get("partial_fraction", 0.0)
            full_lots = self.config["lot_size"]
        if not trigger_pips or fraction <= 0:
            return

        is_buy = position.type == mt5.POSITION_TYPE_BUY
        entry = position.price_open
        trigger_distance = trigger_pips * self.config["pip"]
        # Value the position at the price it could be CLOSED at -- bid for a long,
        # ask for a short. Using the other side books a trigger the market has not
        # actually offered yet, by exactly one spread.
        mark = tick.bid if is_buy else tick.ask
        reached = (mark >= entry + trigger_distance) if is_buy \
            else (mark <= entry - trigger_distance)
        if not reached:
            return

        if position.volume >= full_lots - 1e-9:
            want = self._round_volume(position.volume * fraction, info)
            remainder = round(position.volume - want, 8)
            vol_min = getattr(info, "volume_min", 0.01) or 0.01
            if want > 0 and remainder >= vol_min - 1e-9:
                self._close_partial(position, want, info)
            else:
                # 0.01 lots cannot be halved at any broker. Skip the scale-out and
                # still protect the position, rather than skipping the whole rule.
                log("%s: #%s volume %.2f too small to split (min %.2f); "
                    "moving stop to break-even only"
                    % (self.symbol, position.ticket, position.volume, vol_min))

        needs_be = (position.sl <= 0
                    or (is_buy and position.sl < entry - 1e-9)
                    or (not is_buy and position.sl > entry + 1e-9))
        if needs_be:
            # Only ever widened, never tightened, is the rule for entry stops -- but
            # this one is deliberately TIGHTENED, so it must be checked against the
            # broker's minimum distance from the CURRENT price, not from entry. If
            # price has not travelled far enough for entry to be a legal stop yet,
            # leave it and retry on the next cycle.
            min_dist = (getattr(info, "trade_stops_level", 0) or 0) * info.point
            legal = (mark - entry >= min_dist) if is_buy else (entry - mark >= min_dist)
            if not legal:
                return
            if self._modify_sl(position, entry, info):
                log("%s: #%s stop moved to break-even %.5f"
                    % (self.symbol, position.ticket, entry))

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

                positions = self.bot_positions()   # S1: this bot's positions only

                # S7: position MANAGEMENT runs every cycle, not once per bar. The
                # break-even trigger is an intrabar event, so gating it on a new bar
                # would miss any move that handed the profit back before the close.
                # Entries stay gated below -- S4 was about entries firing repeatedly
                # inside one candle, and that guard is untouched.
                if positions:
                    tick = mt5.symbol_info_tick(self.symbol)
                    info = self._symbol_info()
                    if tick is not None and info is not None:
                        for pos in positions:
                            self.manage_position(pos, info, tick)

                # S4: enter and exit at most once per closed bar.
                if bar_time == self._last_bar_time:
                    self._sleep(15)
                    continue

                # Re-read: a scale-out above changed the volume of these positions,
                # and close_position() sends position.volume. Closing a stale 0.1
                # against a position holding 0.05 is rejected as invalid volume.
                positions = self.bot_positions()

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


def simulate_legacy(df, outs, uppers, lowers, config, initial_balance,
                    volume_min=0.01, volume_step=0.01):
    """The ORIGINAL close-only backtest -- now modelling the scale-out rule too.

    Still close-only and still cost-free: this is the engine POST /backtest has
    always run, and `backend/backtest/engine.py` is the honest one. What changed is
    that it no longer ignores the rule the live bot actually runs. A backtest that
    sizes and exits differently from live cannot answer the question the Backtest
    page is now being asked -- "what would 0.1 out of 0.05 have done?" -- so the
    numbers here MOVED with this change and are not comparable to reports produced
    before it.

    Modelled the way the rest of this engine is: the trigger, the stop and the
    target all fill AT THEIR LEVEL on any bar whose CLOSE has passed them, which is
    optimistic in exactly the direction `warning` describes. The scale-out is
    resolved before the exits because it sits between entry and target and is
    therefore reached first on the way out.

    Pure on purpose -- no MT5 calls -- so the rule can be tested against synthetic
    bars, including a proof that partial_fraction=0 reproduces the original engine
    trade for trade.
    """
    pip = config["pip"]
    sl_dist = config["sl_pips"] * pip
    tp_dist = config["tp_pips"] * pip
    be_dist = config.get("be_trigger_pips", 0) * pip
    lot_size = float(config["lot_size"])
    fraction = float(config.get("partial_fraction", 0.0))
    profit_mult = config["profit_mult"]

    # Decided once: lot_size is constant across a run, so whether the broker could
    # split it at all is a property of the settings, not of any one trade.
    scale_out_lots, runner_lots = _split_lots(lot_size, fraction, volume_min, volume_step)
    # Matches manage_position(): a size too small to split still gets the
    # break-even stop, it just banks nothing.
    arms_breakeven = be_dist > 0 and fraction > 0

    balance = initial_balance
    trades_opened = 0
    wins = 0
    losses = 0
    total_pl = 0.0
    max_drawdown = 0.0
    peak_balance = initial_balance
    partials_fired = 0
    partial_pl = 0.0

    side = 0              # 0 flat, +1 long, -1 short -- lets one branch serve both
    entry_price = 0.0
    open_volume = 0.0     # lots still running; the scale-out reduces it
    be_armed = False      # trigger reached: stop is at entry, partial already taken
    trade_pl = 0.0        # realised on THIS trade so far, partial included

    for i in range(len(df)):
        price = df['close'].iloc[i]
        out = outs[i]
        upper = uppers[i]
        lower = lowers[i]

        if np.isnan(out) or np.isnan(upper) or np.isnan(lower):
            continue

        if side == 0:
            if price < lower or price > upper:
                side = 1 if price < lower else -1
                entry_price = price
                open_volume = lot_size
                be_armed = False
                trade_pl = 0.0
                trades_opened += 1
        else:
            if arms_breakeven and not be_armed \
                    and side * (price - entry_price) >= be_dist:
                be_armed = True
                if scale_out_lots > 0:
                    trigger_price = entry_price + side * be_dist
                    pl = (side * (trigger_price - entry_price)
                          * scale_out_lots * profit_mult)
                    balance += pl
                    total_pl += pl
                    trade_pl += pl
                    partial_pl += pl
                    partials_fired += 1
                    open_volume = runner_lots

            # be_armed pulls the stop to entry, which is the whole point of the
            # rule and the reason it clips winners: the runner now exits flat where
            # it would otherwise have had the full stop's room to recover.
            stop_price = entry_price if be_armed else entry_price - side * sl_dist
            target_price = entry_price + side * tp_dist

            exit_price = None
            if side * (price - stop_price) <= 0:
                exit_price = stop_price
            elif side * (price - target_price) >= 0:
                exit_price = target_price
            elif side * (price - out) >= 0:
                exit_price = price

            if exit_price is not None:
                pl = side * (exit_price - entry_price) * open_volume * profit_mult
                balance += pl
                total_pl += pl
                trade_pl += pl
                # One win or loss per TRADE, scored on the partial and the runner
                # together -- counting the banked partial as its own win is what
                # makes a scale-out look like it raises the hit rate for free.
                if trade_pl > 0:
                    wins += 1
                elif trade_pl < 0:
                    losses += 1
                side = 0

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
        "max_drawdown": max_drawdown,
        "lot_size": lot_size,
        "partial_fraction": fraction,
        "scale_out_lots": scale_out_lots,
        "runner_lots": runner_lots,
        "partials_fired": partials_fired,
        "partial_pl": partial_pl,
        # This engine checks exits on CLOSES only and models no spread, commission
        # or slippage. It DOES now model the scale-out / break-even rule, but at an
        # assumed fill on the trigger level, so it still flatters that rule too.
        "warning": (
            "Close-only, cost-free engine: no spread/commission/slippage and no "
            "intrabar stops -- the stop, the target and the scale-out are all "
            "assumed to fill at their level on the bar that closes past them. "
            "Results are optimistic and do not reflect live behaviour. Use "
            "`python -m backend.scripts.run_baseline` for decisions."
        ),
    }


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

    # ---- sizing -------------------------------------------------------------

    def get_settings(self, symbol):
        """Current sizing for `symbol`, in the lots the dashboard edits.

        `scale_out_lots` is DERIVED from partial_fraction on the way out and never
        stored, for the reason in scale_out_fraction(): the rule is a proportion,
        and a lot count would stop meaning the same thing the moment the size
        changed. `risk_per_lot` is here so the form can show what a size actually
        costs -- this bot has no equity-based sizing and no daily loss cap, so the
        dollar figure behind "0.1" is the only warning a user gets.
        """
        cfg = SYMBOL_CONFIG.get(symbol)
        if cfg is None:
            raise ConfigRejected("Unknown symbol %r. Configured: %s"
                                 % (symbol, ", ".join(SUPPORTED_SYMBOLS)))
        vol_min, vol_max, step, from_broker = _volume_limits(symbol)
        with _CONFIG_LOCK:
            lot_size = float(cfg["lot_size"])
            fraction = float(cfg.get("partial_fraction", 0.0))
        scale_out, runner = _split_lots(lot_size, fraction, vol_min, step)
        open_positions = len(bot_positions(symbol))
        return {
            "symbol": symbol,
            "lot_size": lot_size,
            "partial_fraction": fraction,
            "scale_out_lots": scale_out,
            "runner_lots": runner,
            # fraction > 0 but nothing to bank: the size cannot be split at this
            # broker, so the trigger only moves the stop. Say so rather than let
            # the form show a 0.00 that looks like the rule is off.
            "splittable": bool(fraction > 0 and scale_out > 0),
            "be_trigger_pips": cfg.get("be_trigger_pips", 0),
            "sl_pips": cfg["sl_pips"],
            "tp_pips": cfg["tp_pips"],
            "pip": cfg["pip"],
            "risk_per_lot": cfg["sl_pips"] * cfg["pip"] * cfg["profit_mult"],
            "volume_min": vol_min,
            "volume_max": vol_max,
            "volume_step": step,
            "broker_limits": from_broker,
            "open_positions": open_positions,
            "locked": bool(open_positions),
        }

    def update_settings(self, symbol, lot_size=None, scale_out_lots=None):
        """Apply a sizing edit from the UI. Returns the new settings + any notes.

        Either field may be omitted to leave it alone, but they are validated
        against each other: the scale-out is stored as a fraction of whatever lot
        size ends up applied, so submitting both together is the normal path.
        """
        cfg = SYMBOL_CONFIG.get(symbol)
        if cfg is None:
            raise ConfigRejected("Unknown symbol %r. Configured: %s"
                                 % (symbol, ", ".join(SUPPORTED_SYMBOLS)))
        vol_min, vol_max, step, from_broker = _volume_limits(symbol)
        notes = []

        with _CONFIG_LOCK:
            # Refused while a position is open, because manage_position() decides
            # "has the scale-out already fired?" by comparing the position's volume
            # against lot_size. Lower the size mid-trade and an already-reduced
            # position looks untouched, so it is scaled out a second time. The
            # alternative -- remembering the size per ticket -- would have to
            # survive restarts, reconnects and BotManager replacing the thread,
            # which is exactly the state that rule was written to avoid.
            open_now = len(bot_positions(symbol))
            if open_now:
                raise ConfigRejected(
                    "%s has %d open position(s). Sizing can only be changed while "
                    "flat -- changing it now would re-scale a trade that is already "
                    "running. Wait for it to close, or stop the bot and close it."
                    % (symbol, open_now))

            new_lot = float(cfg["lot_size"]) if lot_size is None else float(lot_size)
            if not math.isfinite(new_lot) or new_lot <= 0:
                raise ConfigRejected("Lot size must be a positive number.")
            if new_lot > vol_max + 1e-9:
                raise ConfigRejected("Lot size %g is above the broker maximum of %g."
                                     % (new_lot, vol_max))
            if new_lot < vol_min - 1e-9:
                raise ConfigRejected("Lot size %g is below the broker minimum of %g."
                                     % (new_lot, vol_min))
            snapped = _snap_volume(new_lot, vol_min, vol_max, step)
            if abs(snapped - new_lot) > 1e-9:
                notes.append("Lot size rounded to %g -- the volume step is %g."
                             % (snapped, step))
            new_lot = snapped

            if scale_out_lots is None:
                new_fraction = float(cfg.get("partial_fraction", 0.0))
                # The lot size may just have moved under a fraction that was set
                # against the old one. That is the case scale_out_fraction() warns
                # about, so make it visible instead of leaving it implicit.
                if lot_size is not None and new_fraction > 0:
                    notes.append("Scale-out kept at %.0f%% of the position, now %g lots."
                                 % (new_fraction * 100.0, new_lot * new_fraction))
            else:
                requested = float(scale_out_lots)
                new_fraction = scale_out_fraction(new_lot, requested)
                if requested > 0:
                    fills, runner = _split_lots(new_lot, new_fraction, vol_min, step)
                    if fills <= 0:
                        notes.append(
                            "%g lots cannot be split at this broker (minimum %g, "
                            "step %g), so the trigger will move the stop to "
                            "break-even and bank nothing."
                            % (new_lot, vol_min, step))
                    elif abs(fills - requested) > 1e-9:
                        notes.append("Scale-out will fill %g lots -- the volume step "
                                     "is %g." % (fills, step))

            cfg["lot_size"] = new_lot
            cfg["partial_fraction"] = new_fraction
            _save_settings()

        log("%s: sizing set to lot_size=%g partial_fraction=%.4f (%g lots out)"
            % (symbol, new_lot, new_fraction, new_lot * new_fraction))
        result = self.get_settings(symbol)
        result["notes"] = notes
        return result

    # ---- backtest -----------------------------------------------------------

    def run_backtest(self, symbol, start_date, end_date, initial_balance,
                     lot_size=None, partial_fraction=None):
        """Fetch bars and run the legacy engine. `lot_size` / `partial_fraction`
        override the live settings for this run only -- nothing is written back."""
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
        with _CONFIG_LOCK:
            config = dict(SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG["XAUUSDm"]))
        if lot_size is not None:
            config["lot_size"] = _validated("lot_size", lot_size)
        if partial_fraction is not None:
            config["partial_fraction"] = _validated("partial_fraction", partial_fraction)
        outs, uppers, lowers = bot.calculate_envelope(df)

        vol_min, _vol_max, step, _from_broker = _volume_limits(symbol)
        return simulate_legacy(df, outs, uppers, lowers, config, initial_balance,
                               volume_min=vol_min, volume_step=step)

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
