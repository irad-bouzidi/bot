import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import math
import os
import time
import threading
from datetime import datetime, timedelta
from typing import Dict

from backend.core.errors import ConfigRejected, DatabaseUnavailable
from backend.core.symbols import (
    BOOL_KEYS, EDITABLE_KEYS, SUPPORTED_SYMBOLS, SYMBOL_CONFIG, price_levels,
)
from backend.db import pool as db_pool
from backend.db import repository as repo
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

# The schema version this build's queries require, not just "any schema". Bump
# it whenever repository.py starts naming a column that an older database does
# not have, so init_persistence() can refuse with the migrate command instead of
# letting psycopg2 raise UndefinedColumn from somewhere deeper.
REQUIRED_SCHEMA_VERSION = 3

# Max price slippage tolerated on a market order, in points.
DEVIATION_POINTS = 20

# symbol_info().filling_mode is a BITMASK and does not share values with the
# mt5.ORDER_FILLING_* order constants. Keep them separate.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)

# Symbol configurations, EDITABLE_KEYS and SUPPORTED_SYMBOLS all live in
# backend/core/symbols.py now. They moved because the same table is needed by
# `backend.db.migrate` (no terminal) and by `backend.scripts.run_baseline` (no
# terminal and no database), and neither may import this module -- it imports
# MetaTrader5. Re-exported here because every existing caller, and every test,
# reads them off `backend.bot_manager`.
#
# SYMBOL_CONFIG is the SAME dict object, not a copy: update_settings() mutates it
# in place and a running bot holds a live reference into it (TradingBot.config),
# which is how a size edit reaches a thread without restarting it.
# (the import itself is at the top of the file, with the others)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Where the sizing USED to live. Kept only so init_persistence() can report a
# file that was never imported; nothing reads it for values any more --
# `python -m backend.db.migrate` copies it into Postgres once. It is not
# deleted, so checking out the previous commit still finds the chosen size.
LEGACY_SETTINGS_FILE = os.environ.get(
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
    if key in BOOL_KEYS:
        # Deliberately NOT bool(value): bool("false") is True, so a string that
        # reached the column through psql, a migration or an old settings.json
        # would turn the rule ON while every place that displays it said off --
        # the failure mode is a runner being closed before the target by a rule
        # the dashboard reports as disabled. isinstance(True, int) is also True,
        # so bool has to be tested before int.
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ConfigRejected("%s must be true or false" % key)
    if isinstance(value, bool):
        # New hazard, created by this function seeing booleans at all: bool is a
        # subclass of int, so float(True) is 1.0. A boolean landing under
        # `lot_size` would validate cleanly as 1.0 lots -- ten times the shipped
        # size, ~$700 a trade on gold -- and every range check below would pass
        # it. Refuse the type rather than the value.
        raise ConfigRejected("%s must be a number, not true/false" % key)
    value = float(value)
    if not math.isfinite(value):
        raise ConfigRejected("%s must be a finite number" % key)
    if key == "lot_size" and value <= 0:
        raise ConfigRejected("lot_size must be positive")
    if key == "partial_fraction" and not 0.0 <= value < 1.0:
        raise ConfigRejected("partial_fraction must be in [0, 1)")
    return value


def _load_settings():
    """Apply the persisted sizing over SYMBOL_CONFIG.

    Deliberately narrow, and no less so for being a database rather than a
    file: only EDITABLE_KEYS, only for symbols already defined in code, and
    only values that survive `_validated`. A row must never be able to
    introduce a symbol or move a stop -- and unlike the old settings.json, a
    database is writable by psql, by a migration, and by anything else holding
    the DSN, so the narrowness carries more weight here rather than less.

    Called by init_persistence(), NOT at import: importing this module must not
    require a reachable Postgres, or `pytest` and every offline research script
    would need one.
    """
    def _reject(symbol, key, exc):
        log("settings: ignoring %s.%s -- %s" % (symbol, key, exc))

    loaded = repo.load_settings(list(SYMBOL_CONFIG), validate=_validated,
                               on_reject=_reject)
    with _CONFIG_LOCK:
        for symbol, values in loaded.items():
            cfg = SYMBOL_CONFIG.get(symbol)
            if cfg is None:
                continue
            for key in EDITABLE_KEYS:
                if key in values:
                    cfg[key] = values[key]
            # exit_at_mean is formatted as on/off, not %g: it is the one
            # EDITABLE_KEYS value that is not a number, and a boot log reading
            # "exit_at_mean=1" would be the same ambiguity _validated() refuses.
            log("settings: %s loaded lot_size=%g partial_fraction=%g "
                "exit_at_mean=%s"
                % (symbol, cfg["lot_size"], cfg.get("partial_fraction", 0.0),
                   "on" if cfg.get("exit_at_mean") else "off"))
    return loaded


def _save_settings(symbol, notes=None):
    """Persist one symbol's EDITABLE_KEYS. Call with _CONFIG_LOCK held.

    Takes a symbol now, where the file version rewrote the whole document. The
    write is a single statement that also appends the audit row, so a
    concurrent save cannot interleave -- the equivalent of the old
    write-then-rename, moved into the transaction where it belongs.
    """
    cfg = SYMBOL_CONFIG[symbol]
    return repo.save_settings(symbol, cfg["lot_size"],
                              cfg.get("partial_fraction", 0.0),
                              bool(cfg.get("exit_at_mean", False)),
                              source="api", notes=notes)


def _persist(what, fn, *args, **kwargs):
    """Best-effort DB write from inside the live loop. Never raises.

    A Postgres outage must NOT stop a bot that is holding a position. The size
    it trades with is already in SYMBOL_CONFIG in memory, so an outage costs
    the record and nothing else; halting would leave a real position running
    with nothing to fire its scale-out or move its stop to break-even, which is
    strictly worse than a gap in the history. The gap is reported instead --
    through GET /health and the returned False.
    """
    try:
        fn(*args, **kwargs)
        return True
    except Exception as exc:
        log("db: could not %s -- %r" % (what, exc))
        return False


def init_persistence(require_schema=True):
    """Connect to Postgres, check the schema, and adopt the stored sizing.

    Called once from the FastAPI startup hook, and it is allowed to FAIL THE
    BOOT. That is the point: `lot_size` is the only risk control this bot has,
    and starting with the 0.1 code default because the database was unreachable
    would restore ~$70/trade of exposure for someone who had deliberately
    lowered it. The old settings file went to the trouble of a
    write-then-rename to avoid exactly that; degrading to the default here
    would give it away.
    """
    if require_schema:
        version = repo.schema_version()
        # A FLOOR, not merely "some schema". load_settings() now SELECTs
        # `exit_at_mean`, so a database left at version 2 fails inside
        # _load_settings() with an UndefinedColumn from psycopg2 -- past the
        # point where this function can still name the command to run, which is
        # the whole job of the message below.
        if version < REQUIRED_SCHEMA_VERSION or not repo.tables_present():
            raise DatabaseUnavailable(
                "Postgres at %s is at schema version %d; this build needs %d. "
                "Run: python -m backend.db.migrate"
                % (db_pool.redact(db_pool.database_url()), version,
                   REQUIRED_SCHEMA_VERSION))
    repo.ensure_bot_rows(list(SYMBOL_CONFIG))
    loaded = _load_settings()
    for symbol in SYMBOL_CONFIG:
        if symbol in loaded:
            continue
        # A configured symbol with no row would fall back to the code default
        # on every read, which is the silent restore this whole path exists to
        # prevent. Write the default down once, explicitly, so what the bot
        # will trade is a stored decision rather than an absence.
        with _CONFIG_LOCK:
            _save_settings(symbol, notes="seeded from SYMBOL_CONFIG on first boot")
        log("settings: %s had no stored row; seeded the code default" % symbol)
    if os.path.isfile(LEGACY_SETTINGS_FILE):
        log("settings: %s is no longer read; Postgres is the store "
            "(python -m backend.db.migrate imports it once)"
            % LEGACY_SETTINGS_FILE)
    return loaded


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


def _finite(value):
    """A float for Postgres, or None if it is NaN/inf.

    numpy's NaN adapts to the SQL literal NaN, which a DOUBLE PRECISION column
    accepts -- so an un-guarded band would be STORED as NaN and read back out
    to the dashboard as a number that compares False against every price. The
    NaN case is already handled explicitly a few lines later; this keeps it
    from being persisted in the meantime.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _deal_entry_kind(value):
    """mt5.DEAL_ENTRY_* -> the string stored in deals.entry_kind.

    Mapped to names rather than stored as the raw integer so the SQL fold reads
    as `entry_kind = 'in'`. The constants are looked up with getattr because
    DEAL_ENTRY_OUT_BY is absent on some MetaTrader5 package versions, and an
    AttributeError at import would take the whole live loop down.
    """
    for name, label in (("DEAL_ENTRY_IN", "in"),
                        ("DEAL_ENTRY_OUT", "out"),
                        ("DEAL_ENTRY_INOUT", "inout"),
                        ("DEAL_ENTRY_OUT_BY", "out_by"),
                        ("DEAL_ENTRY_STATE", "state")):
        constant = getattr(mt5, name, None)
        if constant is not None and value == constant:
            return label
    return "other"


def _deal_type_name(value):
    """mt5.DEAL_TYPE_* -> 'buy' / 'sell' / 'other'.

    Only the entry deal's value is ever interpreted (a long's exit deal is a
    sell, so reading side off an exit would invert every trade); balance,
    credit and commission deals fall through to 'other' and are excluded from
    the fold by having no position of their own.
    """
    if value == getattr(mt5, "DEAL_TYPE_BUY", -1):
        return "buy"
    if value == getattr(mt5, "DEAL_TYPE_SELL", -2):
        return "sell"
    return "other"


def _deal_rows(history, symbol):
    """MT5 deal objects -> rows for `deals`, restricted to THIS bot's positions.

    S1 again, in the reporting path: history_deals_get returns every deal on
    the symbol whatever opened it, and storing the lot would put the user's
    manual trades on the bot's card and in its win rate.

    Filtering by magic alone is not enough. A position closed by its own SL or
    TP produces a deal generated by the broker's stop order, and its magic is
    not guaranteed to carry the opening order's. So the pass is two-stage:
    collect the position ids that have at least one deal bearing MAGIC_NUMBER,
    then keep every deal on those positions. Without that, an SL-closed trade
    would keep its entry and lose its exit -- and would sit in `trades`
    permanently open, with the loss never counted.
    """
    ours = set()
    for deal in history:
        position_id = getattr(deal, "position_id", 0) or 0
        if position_id and getattr(deal, "magic", 0) == MAGIC_NUMBER:
            ours.add(position_id)

    rows = []
    for deal in history:
        position_id = getattr(deal, "position_id", 0) or 0
        if position_id not in ours:
            continue
        rows.append({
            "ticket": int(deal.ticket),
            "order_ticket": int(getattr(deal, "order", 0) or 0) or None,
            "position_id": int(position_id),
            "symbol": getattr(deal, "symbol", "") or symbol,
            "magic": int(getattr(deal, "magic", 0) or 0),
            "entry_kind": _deal_entry_kind(getattr(deal, "entry", None)),
            "deal_type": _deal_type_name(getattr(deal, "type", None)),
            "volume": float(getattr(deal, "volume", 0.0) or 0.0),
            "price": float(getattr(deal, "price", 0.0) or 0.0),
            "profit": float(getattr(deal, "profit", 0.0) or 0.0),
            "commission": float(getattr(deal, "commission", 0.0) or 0.0),
            "swap": float(getattr(deal, "swap", 0.0) or 0.0),
            "fee": float(getattr(deal, "fee", 0.0) or 0.0),
            "comment": getattr(deal, "comment", None) or None,
            # MT5 reports deal times as a UNIX timestamp in the SERVER's clock.
            # Stored as UTC so two hosts in different timezones agree on the
            # ordering the trade fold depends on.
            "dealt_at": datetime.utcfromtimestamp(int(getattr(deal, "time", 0) or 0)),
        })
    return rows


# NOTE: no _load_settings() here any more. It needs Postgres, and importing this
# module must not: `pytest`, the research scripts and `python -m backend.db.migrate`
# all import their way past this line with no database running. The API calls
# init_persistence() from its startup hook instead, where a failure can refuse
# the boot rather than silently trade the code default.

class TradingBot(threading.Thread):
    def __init__(self, symbol: str):
        super().__init__()
        self.daemon = True  # S6: never block interpreter shutdown
        self.symbol = symbol
        # A live REFERENCE into SYMBOL_CONFIG, not a copy: update_settings() edits
        # that dict in place so a running bot picks the new size up on its next
        # entry without being restarted. Read it under _CONFIG_LOCK where the value
        # has to stay consistent with a position (see open_trade).
        #
        # An unknown symbol used to fall back to XAUUSDm's row. That was survivable
        # while gold was the only symbol; with a second one it is a live hazard,
        # because the fallback is silent and the numbers are not interchangeable --
        # gold's pip is 0.1, so a BTCUSDm typo would compute a $0.70 stop on an
        # $81,000 instrument and send it. Refuse instead.
        if symbol not in SYMBOL_CONFIG:
            raise ConfigRejected("Unknown symbol %r. Configured: %s"
                                 % (symbol, ", ".join(SUPPORTED_SYMBOLS)))
        self.config = SYMBOL_CONFIG[symbol]
        self.running = False
        self._stop_event = threading.Event()

        # S4: the once-per-closed-bar and COOLDOWN_BARS guards. Still held in
        # memory because that is the hot path, but SEEDED FROM and WRITTEN
        # THROUGH TO `bot_state` (see _load_bar_marks / _mark_bar).
        #
        # As instance attributes alone they started at None on every thread, so
        # pressing Stop then Start cleared the cooldown and the bot could enter
        # again on the very bar it had just entered on -- the repeat-fire S4
        # exists to prevent, reachable from the dashboard's own buttons.
        #
        # Memory stays the working copy on purpose: if Postgres goes down the
        # guards keep working for the life of the process, so an outage costs
        # the durability and not the guard.
        self._last_bar_time = None
        self._last_entry_bar = None

        # NOTE: there is no self.stats dict any more. Wins, losses, P&L and the
        # trade count are SELECTs over `trades` (repo.trade_stats), which is
        # folded from the raw MT5 deals -- derived, never accumulated, so a
        # restart or a re-scanned history window cannot drift them. The
        # envelope reading goes to `bot_snapshots` each cycle.

    def stop(self):
        self.running = False
        self._stop_event.set()

    # ---- persisted per-bot state ---------------------------------------------

    def _load_bar_marks(self):
        """Adopt the stored S4 guards. Called once, as the thread starts."""
        try:
            last_bar, last_entry = repo.get_bar_marks(self.symbol)
        except Exception as exc:
            log("%s: could not read the stored bar marks -- %r" % (self.symbol, exc))
            return
        self._last_bar_time = last_bar
        self._last_entry_bar = last_entry
        if last_bar is not None or last_entry is not None:
            log("%s: resumed bar marks last_bar=%s last_entry=%s"
                % (self.symbol, last_bar, last_entry))

    def _mark_bar(self, last_bar_time=None, last_entry_bar=None):
        """Set a guard in memory and write it through. Best-effort on the write."""
        if last_bar_time is not None:
            self._last_bar_time = last_bar_time
        if last_entry_bar is not None:
            self._last_entry_bar = last_entry_bar
        _persist("mark bars for %s" % self.symbol, repo.set_bar_marks,
                 self.symbol, last_bar_time=last_bar_time,
                 last_entry_bar=last_entry_bar)

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
        log("%s: opened %s @ %.5f sl=%.5f tp=%.5f" % (self.symbol, action, price, sl, tp))
        # The trade count is no longer incremented here. It was a counter on the
        # thread, so it reset on every restart and counted an entry the broker
        # might reject downstream; it is now COUNT(*) over `trades`, folded from
        # the deals the broker actually reports.
        #
        # Reconciling here just brings the row forward -- the deal may not be in
        # history yet, in which case the once-a-minute pass picks it up. The open
        # position already shows on the card either way, from positions_get.
        #
        # Wrapped, and OUTSIDE the _CONFIG_LOCK, because the order has already
        # gone through: reporting must never be able to fail an entry that the
        # broker accepted, nor hold the sizing lock across an IPC history scan.
        try:
            self.reconcile_trades()
        except Exception as exc:
            log("%s: entry placed, but the history refresh failed -- %r"
                % (self.symbol, exc))
        return True

    def close_position(self, position, comment="Closing NW Bot"):
        """Close a whole position at market.

        `comment` reaches `deals.comment`, which the trade fold carries through
        to `trades.comment`, so a centre-line exit is distinguishable from any
        other close after the fact. Without it they are identical in the stored
        history and the cost of a rule that closes trades can never be measured
        -- which is the one thing `Trading Bot.md` insists on for any rule that
        changes which trades exist. MT5 truncates order comments around 31
        characters, so keep them short.
        """
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
            "comment": comment,
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

    def _mean_reversion_exit(self, positions, current_close, out):
        """The centre-line exit -- OFF unless `exit_at_mean` is set for this symbol.

        It used to be unconditional, and it has no scale-out awareness: no volume
        check, no be_armed flag, nothing that notices the position already banked
        a partial and pulled its stop to entry. `out` is the envelope's CENTRE,
        which sits about `mult * mae` from entry -- ~6.00 on gold, PAST the 5.00
        scale-out trigger and SHORT of the 10.00 target. So a trade that did
        everything the scale-out rule intended was then closed here, and the
        target it had been left running for was unreachable in practice. One live
        XAUUSDm short entered at 4485.183 (SL 4492.183, TP 4475.183) banked half
        at 4480.183 and closed here at 4479.196.

        Extracted from run() so it can be tested: nothing drives the live loop in
        the suite, so for as long as this was six inlined lines the rule that
        decided most exits had no test at all.

        Still behind the S4 once-per-closed-bar gate, and still not a stop -- it
        reads the CLOSE, so it can neither replace the broker-side SL nor be
        relied on intrabar.
        """
        # Read under the lock, unlike `pip` in manage_position(): this key is
        # editable at runtime AND editable while a position is open, so the loop
        # can genuinely race a save.
        with _CONFIG_LOCK:
            enabled = bool(self.config.get("exit_at_mean", False))
        if not enabled:
            return
        for pos in positions:
            if pos.type == mt5.POSITION_TYPE_BUY and current_close >= out:
                self.close_position(pos, comment="NW mean reversion")
            elif pos.type == mt5.POSITION_TYPE_SELL and current_close <= out:
                self.close_position(pos, comment="NW mean reversion")

    def reconcile_trades(self, full=False):
        """Copy this bot's MT5 deals into Postgres and re-fold them into trades.

        Replaces update_performance_stats(), which summed P&L into an in-memory
        dict on every pass. Two things change.

        **The window shrinks.** It scanned a full 365 days over IPC every time.
        Here the default window starts a little before the newest stored deal,
        because MT5 credits `swap` and `commission` to a deal after the fact and
        an upsert has to be able to correct a row it has already seen. `full=True`
        re-scans the whole year, which is what the first run after this change
        does to backfill the history that was never stored.

        **The unit becomes the position, not the closing deal.** The old loop
        counted every DEAL_ENTRY_OUT as its own win or loss, so a trade that
        scaled out and then stopped at break-even booked one win plus one flat --
        two outcomes for one trade, the win rate lifted by the scale-out and no
        loss recorded against it. That flattered exactly the rule the cached
        gold data measures as NEGATIVE for expectancy (see CLAUDE.md). The fold
        into `trades` groups by position_id, so one trade is one outcome decided
        on net profit.
        """
        now = datetime.now()
        from_date = now - timedelta(days=365)
        if not full:
            try:
                newest = repo.latest_deal_time(self.symbol)
            except Exception as exc:
                log("%s: could not read the stored deal history -- %r"
                    % (self.symbol, exc))
                return False
            if newest is not None:
                # Overlap the window rather than resuming exactly at the newest
                # deal: `swap` lands on a deal later, and the deal that closes a
                # position can be timestamped before one already stored for
                # another. A day of re-read costs nothing -- the upsert is keyed
                # on the broker's own deal ticket.
                candidate = newest.replace(tzinfo=None) - timedelta(days=1)
                from_date = max(from_date, candidate)

        history = mt5.history_deals_get(from_date, now, group="*%s*" % self.symbol)
        if history is None:
            log("%s: history_deals_get returned None (%s)"
                % (self.symbol, mt5.last_error()))
            return False

        rows = _deal_rows(history, self.symbol)
        if not rows:
            return True
        ok = _persist("store %d deals for %s" % (len(rows), self.symbol),
                      repo.upsert_deals, rows)
        if not ok:
            return False
        return _persist("rebuild trades for %s" % self.symbol,
                        repo.rebuild_trades, self.symbol)

    def _reconcile_quietly(self, full=False):
        """reconcile_trades() that can only log. Never raises.

        The live loop calls this instead of reconcile_trades() directly, so that
        no failure in the REPORTING path -- a database blip, an MT5 history call
        that throws -- can stop or delay the part of this thread that manages
        real positions. `reconcile_trades` itself already returns False for the
        failures it anticipates; this covers the ones it does not.
        """
        try:
            return self.reconcile_trades(full=full)
        except Exception as exc:
            log("%s: trade history refresh failed -- %r" % (self.symbol, exc))
            return False

    def _sleep(self, seconds):
        """Responsive sleep: wakes immediately on stop()."""
        for _ in range(int(seconds)):
            if self._stop_event.is_set():
                return
            time.sleep(1)

    def run(self):
        self.running = True
        # The S4 guards come back from `bot_state` BEFORE the first cycle, so a
        # Stop/Start (or a process restart) resumes the cooldown instead of
        # clearing it and re-entering on the bar it just entered on.
        self._load_bar_marks()
        _persist("mark %s running" % self.symbol, repo.set_snapshot_status,
                 self.symbol, "Running", None)
        # Backfill on the first pass: nothing before this change stored a deal,
        # so the first reconcile has to read the whole year to build a history
        # that the once-a-minute incremental pass then just keeps current.
        #
        # This runs BEFORE the while loop, so an exception here would kill the
        # thread outright and the bot would never trade -- the same class of
        # failure as reporting "Running" and never trading, just inverted.
        # Reporting does not get to do that.
        self._reconcile_quietly(full=True)
        last_stats_refresh = time.time()

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
                    # Reported on the card, not just in the log. All-NaN bands
                    # compare False against every price, so this is the state in
                    # which the bot says "Running" and never trades.
                    short = ("only %d closed bars, need %d for a valid envelope"
                             % (len(df), MIN_USABLE_BARS))
                    log("%s: %s; not trading" % (self.symbol, short))
                    _persist("record warm-up shortfall for %s" % self.symbol,
                             repo.set_snapshot_status, self.symbol, "Running", short)
                    self._sleep(30)
                    continue

                current_close = df["close"].iloc[-1]
                bar_time = int(df["time"].iloc[-1])
                out_arr, upper_arr, lower_arr = self.calculate_envelope(df)
                out = out_arr[-1]
                upper = upper_arr[-1]
                lower = lower_arr[-1]

                # Fetched ONCE per cycle and reused below. The snapshot wants
                # the count and rule S7 wants the positions themselves; two
                # calls would be two round trips over IPC on the signal path,
                # which is the cost update_performance_stats() was throttled to
                # once a minute to avoid.
                positions = self.bot_positions()   # S1: this bot's positions only

                # The latest reading goes to `bot_snapshots` instead of a dict on
                # this thread. Only the last value of each array is stored: the
                # dashboard reads one number per band, and get_bot_stats() used
                # to carry the whole 1200-element array out of the thread just to
                # take [-1] off it.
                _persist("snapshot %s" % self.symbol, repo.save_snapshot,
                         self.symbol, "Running",
                         last_close=float(current_close),
                         nw_out=_finite(out), nw_upper=_finite(upper),
                         nw_lower=_finite(lower),
                         bar_time=bar_time, open_positions=len(positions),
                         # None: nothing here is blocking. The dashboard renders
                         # this field as a warning on the card
                         # (frontend/src/App.tsx) and the warm-up shortfall and
                         # NaN-envelope paths above still set it -- both are
                         # "Running but not trading", which is what the field is
                         # for.
                         detail=None)

                # Reconcile at most once a minute. Cheaper than the 365-day scan
                # it replaces -- the window now starts near the newest stored
                # deal -- but it is still IPC and still does not belong in the
                # signal path on every cycle.
                now = time.time()
                if now - last_stats_refresh >= 60:
                    # Quietly: a reporting hiccup must not cost a trading cycle.
                    # Unwrapped, it would fall to the loop's own handler, which
                    # sleeps 10s and starts over -- so a database blip would
                    # delay the scale-out check as well as the history.
                    self._reconcile_quietly()
                    last_stats_refresh = now

                if np.isnan(out) or np.isnan(upper) or np.isnan(lower):
                    log("%s: envelope is NaN, skipping this bar" % self.symbol)
                    _persist("record NaN envelope for %s" % self.symbol,
                             repo.set_snapshot_status, self.symbol, "Running",
                             "envelope is NaN -- not trading this bar")
                    self._sleep(15)
                    continue

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
                            self._mark_bar(last_entry_bar=bar_time)
                    elif current_close > upper:
                        if self.open_trade("SELL"):
                            self._mark_bar(last_entry_bar=bar_time)
                else:
                    self._mean_reversion_exit(positions, current_close, out)

                self._mark_bar(last_bar_time=bar_time)
                self._sleep(15)

            except Exception as e:
                log("Bot Error (%s): %r" % (self.symbol, e))
                # Surfaced on the card as well as the log. A thread that keeps
                # looping on the same exception previously showed a healthy
                # "Running" with the reason visible only to whoever was tailing
                # stdout.
                _persist("record error for %s" % self.symbol,
                         repo.set_bot_error, self.symbol, repr(e))
                _persist("record error status for %s" % self.symbol,
                         repo.set_snapshot_status, self.symbol, "Running", repr(e))
                self._sleep(10)

        _persist("mark %s stopped" % self.symbol, repo.set_snapshot_status,
                 self.symbol, "Stopped", None)
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
    # Matches the live loop's gate. Defaults FALSE here too, so a config dict
    # assembled without the key backtests the shipped behaviour rather than the
    # pre-toggle one -- a backtest that modelled an exit the bot no longer takes
    # is the exact mismatch POST /backtest exists to avoid.
    at_mean = bool(config.get("exit_at_mean", False))

    balance = initial_balance
    trades_opened = 0
    wins = 0
    losses = 0
    total_pl = 0.0
    max_drawdown = 0.0
    peak_balance = initial_balance
    partials_fired = 0
    partial_pl = 0.0

    # One entry per CLOSED trade, in the order they closed. It exists so several
    # symbols can be replayed onto ONE account (combine_legacy_results): a
    # combined drawdown cannot be recovered from two finished summaries, because
    # the deepest trough of the merged curve depends on the interleaving, and
    # taking the worse of the two per-symbol figures both understates a
    # simultaneous drawdown and overstates offsetting ones.
    closed_trades = []
    # No `time` column in the synthetic frames the engine's own tests use. None
    # rather than a positional index, so a caller merging two symbols can tell
    # "unknown" from "bar 3" instead of interleaving on a meaningless key.
    times = df["time"] if "time" in df.columns else None

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
            elif at_mean and side * (price - out) >= 0:
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
                # `trade_pl`, not `pl`: the partial is part of this trade's
                # result, and recording the runner alone would let a combined
                # equity curve book the scale-out twice -- once here and once in
                # the symbol's own total.
                closed_trades.append({
                    "closed_at": (times.iloc[i] if times is not None else None),
                    "pl": trade_pl,
                    "scaled_out": be_armed and scale_out_lots > 0,
                })
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
        "closed_trades": closed_trades,
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


def combine_legacy_results(results, initial_balance):
    """Replay several symbols' closed trades onto ONE account, in time order.

    "Both combined" has to mean one account, because that is the only version of
    the question a trader can act on: two symbols funded separately are just two
    backtests printed next to each other. The consequence is that the combined
    figures are NOT the per-symbol ones added up --

      * `max_drawdown` comes from the merged equity curve. Summing or taking the
        worse of the two per-symbol percentages gets it wrong in both directions:
        it understates two drawdowns that happen to land together, and overstates
        two that offset. The interleaving is the whole content of the number, and
        it cannot be recovered from finished summaries -- which is why
        simulate_legacy returns `closed_trades` at all.
      * win/loss counts DO add up, since a trade is won or lost on its own P&L
        regardless of what the other symbol was doing.

    `results` is {symbol: simulate_legacy(...)}. Pure -- no MT5, no database.
    """
    events = []
    for symbol, res in results.items():
        for seq, trade in enumerate(res.get("closed_trades") or []):
            events.append((symbol, seq, trade))

    # Sorted by close time when every trade has one, which is the live path:
    # run_backtest hands simulate_legacy a frame with a `time` column. Synthetic
    # frames have none, and interleaving those on a positional index would invent
    # an ordering across symbols; leave them grouped and say so instead.
    untimed = [e for e in events if e[2].get("closed_at") is None]
    ordered = not untimed
    if ordered:
        events.sort(key=lambda e: (e[2]["closed_at"], e[0], e[1]))

    balance = initial_balance
    peak_balance = initial_balance
    max_drawdown = 0.0
    wins = losses = 0
    total_pl = 0.0
    for _symbol, _seq, trade in events:
        pl = trade["pl"]
        balance += pl
        total_pl += pl
        if pl > 0:
            wins += 1
        elif pl < 0:
            losses += 1
        peak_balance = max(peak_balance, balance)
        if peak_balance != 0:
            max_drawdown = max(max_drawdown,
                               (peak_balance - balance) / peak_balance * 100)

    trades_opened = sum(r["trades_opened"] for r in results.values())
    per_symbol = {}
    for symbol, res in results.items():
        clean = {k: v for k, v in res.items() if k != "closed_trades"}
        per_symbol[symbol] = clean

    return {
        # Request order, not sorted: the dashboard renders the per-symbol table
        # from this and a run of "gold, then Bitcoin" that reads back the other
        # way round looks like a different run.
        "symbols": list(results),
        "combined": True,
        "trades_ordered": ordered,
        "initial_balance": initial_balance,
        "final_balance": balance,
        "total_pl": total_pl,
        "trades_opened": trades_opened,
        "wins": wins,
        "losses": losses,
        # Denominator is trades OPENED, matching the single-symbol engine: a
        # trade still open at the end of the window is counted as taken and as
        # neither won nor lost, so the two numbers stay comparable.
        "win_rate": (wins / trades_opened * 100) if trades_opened > 0 else 0,
        "max_drawdown": max_drawdown,
        "partials_fired": sum(r["partials_fired"] for r in results.values()),
        "partial_pl": sum(r["partial_pl"] for r in results.values()),
        "per_symbol": per_symbol,
        "warning": (
            "Close-only, cost-free engine: no spread/commission/slippage and no "
            "intrabar stops -- the stop, the target and the scale-out are all "
            "assumed to fill at their level on the bar that closes past them. "
            "Combined figures replay both symbols onto ONE account in close-time "
            "order, so the drawdown is the merged curve's and is NOT the sum of "
            "the per-symbol ones. Results are optimistic and do not reflect live "
            "behaviour. Use `python -m backend.scripts.run_baseline` for decisions."
            + ("" if ordered else
               " NOTE: some trades carried no close time, so they could not be "
               "interleaved -- the combined drawdown is unreliable for this run.")
        ),
    }


# Auto-resume is OFF by default, and that is a safety decision rather than an
# oversight. `bot_state.desired_state` records what the user last asked for so a
# restart can SHOW that a bot was running, but starting live trading with real
# money because a process came back up is not something an unauthenticated API
# should decide on its own -- see the Safety section of CLAUDE.md. The dashboard
# surfaces the disagreement and offers the button; a human presses it. Set
# BOT_AUTO_RESUME=1 to opt in.
AUTO_RESUME = os.environ.get("BOT_AUTO_RESUME", "0").strip().lower() in ("1", "true", "yes")


class BotManager:
    def __init__(self):
        self.bots: Dict[str, TradingBot] = {}
        if not mt5.initialize():
            log("MT5 Init Failed: %s" % (mt5.last_error(),))

    # ---- control ------------------------------------------------------------

    def start_bot(self, symbol: str):
        """Start the thread, and record that a start was ASKED FOR.

        The control event is written whether the answer was yes or no: POST
        /control has no authentication, so what it was asked to do is worth
        recording even when it was refused.
        """
        if symbol.upper() not in [s.upper() for s in SUPPORTED_SYMBOLS]:
            detail = ("Bot only supports: %s. Received: %s"
                      % (", ".join(SUPPORTED_SYMBOLS), symbol))
            log(detail)
            _persist("log control event", repo.record_control_event,
                     symbol, "start", False, detail)
            return False

        if symbol in self.bots and self.bots[symbol].is_alive():
            _persist("log control event", repo.record_control_event,
                     symbol, "start", True, "already running")
            return True

        # No stats hand-off across the thread replacement any more. It existed to
        # carry an in-memory counter onto the new thread; the counters now live in
        # `trades` and `bot_snapshots`, which the new thread reads for itself.
        # Losing that dance also loses the defect it papered over: the trade count
        # was per-process, so it disagreed with the broker's own history after any
        # restart, and "never hide losing trades" cannot survive a counter that
        # resets.
        _persist("record desired state", repo.set_desired_state, symbol, "running")
        self.bots[symbol] = TradingBot(symbol)
        self.bots[symbol].start()
        _persist("log control event", repo.record_control_event,
                 symbol, "start", True, None)
        return True

    def stop_bot(self, symbol: str):
        _persist("record desired state", repo.set_desired_state, symbol, "stopped")
        if symbol in self.bots:
            self.bots[symbol].stop()
            _persist("log control event", repo.record_control_event,
                     symbol, "stop", True, None)
            return True
        # Still a real outcome: desired_state is now 'stopped', which is what a
        # restart reads, so the press was not a no-op even with no thread to stop.
        _persist("log control event", repo.record_control_event,
                 symbol, "stop", True, "no running thread")
        return True

    def resume_desired_bots(self):
        """Restart the bots that were running before the process went down.

        Gated on AUTO_RESUME. With it off -- the default -- this only logs what
        it declined to do, and the disagreement between `desired_state` and the
        live status is reported by /stats for the dashboard to act on.
        """
        try:
            states = repo.get_bot_states()
        except Exception as exc:
            log("db: could not read the desired bot states -- %r" % exc)
            return []
        wanted = [sym for sym, row in states.items()
                  if row.get("desired_state") == "running" and sym in SYMBOL_CONFIG]
        if not wanted:
            return []
        if not AUTO_RESUME:
            log("startup: %s was running before shutdown; NOT auto-started "
                "(set BOT_AUTO_RESUME=1 to change that)" % ", ".join(sorted(wanted)))
            return []
        for symbol in sorted(wanted):
            log("startup: BOT_AUTO_RESUME is set -- restarting %s" % symbol)
            self.start_bot(symbol)
        return sorted(wanted)

    def stop_all(self):
        """S6: stop every bot and join briefly, so shutdown cannot hang.

        Deliberately does NOT write desired_state='stopped'. A process going
        down is not the user changing their mind, and overwriting the desired
        state here would erase the one fact a restart needs.
        """
        for symbol, bot in list(self.bots.items()):
            bot.stop()
        for symbol, bot in list(self.bots.items()):
            if bot.is_alive():
                bot.join(timeout=5)
                if bot.is_alive():
                    log("%s: bot thread did not stop within 5s" % symbol)
        mt5.shutdown()

    # ---- reporting ----------------------------------------------------------

    def get_bot_stats(self, symbol: str):
        """Everything the dashboard card shows, read from Postgres.

        Nothing here is accumulated in this process. `status` is the LIVE thread
        state and `desired_state` is what was last asked for; both are returned
        because they can legitimately disagree -- a crashed thread used to keep
        reporting the "Running" its own stats dict still held.
        """
        bot = self.bots.get(symbol)
        alive = bool(bot is not None and bot.is_alive())
        out = {
            "symbol": symbol,
            "status": "Running" if alive else "Stopped",
            "last_close": 0.0,
            "out": 0.0,
            "upper": 0.0,
            "lower": 0.0,
            "trades_opened": 0,
            "wins": 0,
            "losses": 0,
            "total_pl": 0.0,
            "max_drawdown": 0.0,
            "desired_state": "stopped",
            "persisted": False,
        }
        try:
            snapshot = repo.get_snapshots().get(symbol)
            state = repo.get_bot_state(symbol) or {}
            stats = repo.trade_stats(symbol)
        except Exception as exc:
            # Report the outage rather than serving zeros, which are
            # indistinguishable from a healthy bot that has never traded.
            out["error"] = "Trade history unavailable: %s" % exc
            return out

        if snapshot:
            out.update({
                # The bands persist across a stop, so a stopped card shows where
                # the envelope actually was instead of 0.00.
                "last_close": snapshot.get("last_close") or 0.0,
                "out": snapshot.get("nw_out") or 0.0,
                "upper": snapshot.get("nw_upper") or 0.0,
                "lower": snapshot.get("nw_lower") or 0.0,
                "bar_time": snapshot.get("bar_time"),
                "open_positions": snapshot.get("open_positions") or 0,
                "detail": snapshot.get("detail"),
                "updated_at": snapshot.get("updated_at"),
            })
        out.update({
            "trades_opened": stats["trades_total"],
            "trades_open": stats["trades_open"],
            "trades_closed": stats["trades_closed"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "breakeven": stats["breakeven"],
            "scaled_out": stats["scaled_out"],
            "win_rate": stats["win_rate"],
            "total_pl": stats["total_pl"],
            "gross_pl": stats["gross_pl"],
            "costs": stats["costs"],
            "avg_win": stats["avg_win"],
            "avg_loss": stats["avg_loss"],
            # Realised, closed-trade drawdown in account currency -- see
            # repo.trade_stats. The stat this replaces was initialised to 0.0
            # and never written to at all.
            "max_drawdown": stats["max_drawdown"],
            "last_closed_at": stats["last_closed_at"],
            "desired_state": state.get("desired_state", "stopped"),
            "last_error": state.get("last_error"),
            "last_bar_time": state.get("last_bar_time"),
            "last_entry_bar": state.get("last_entry_bar"),
            "persisted": True,
        })
        return out

    def reconcile_all(self, full=False):
        """Refresh the stored trade history for every configured symbol.

        Called at boot, so the dashboard has a history before any bot is
        started. The old code only ever learned about trades from a running
        thread, so a fresh process reported zero trades until someone pressed
        Start -- on an account that may have been trading for a year.
        """
        done = {}
        for symbol in SUPPORTED_SYMBOLS:
            bot = self.bots.get(symbol) or TradingBot(symbol)
            try:
                done[symbol] = bot.reconcile_trades(full=full)
            except Exception as exc:
                log("%s: reconcile failed -- %r" % (symbol, exc))
                done[symbol] = False
        return done

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
            at_mean = bool(cfg.get("exit_at_mean", False))
        scale_out, runner = _split_lots(lot_size, fraction, vol_min, step)
        open_positions = len(bot_positions(symbol))
        return {
            "symbol": symbol,
            "lot_size": lot_size,
            "partial_fraction": fraction,
            "exit_at_mean": at_mean,
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
            "risk_per_lot": price_levels(symbol)["risk_per_lot"],
            "volume_min": vol_min,
            "volume_max": vol_max,
            "volume_step": step,
            "broker_limits": from_broker,
            "open_positions": open_positions,
            # SIZING is locked while a position is open -- see update_settings
            # for why. `exit_at_mean` is deliberately NOT covered by this flag:
            # it cannot re-scale a running position, and the moment someone
            # reaches for that switch is while a trade is open.
            "locked": bool(open_positions),
        }

    def update_settings(self, symbol, lot_size=None, scale_out_lots=None,
                        exit_at_mean=None):
        """Apply a settings edit from the UI. Returns the new settings + any notes.

        Any field may be omitted to leave it alone, but the two sizing fields are
        validated against each other: the scale-out is stored as a fraction of
        whatever lot size ends up applied, so submitting both together is the
        normal path.
        """
        cfg = SYMBOL_CONFIG.get(symbol)
        if cfg is None:
            raise ConfigRejected("Unknown symbol %r. Configured: %s"
                                 % (symbol, ", ".join(SUPPORTED_SYMBOLS)))
        vol_min, vol_max, step, from_broker = _volume_limits(symbol)
        notes = []
        touches_sizing = lot_size is not None or scale_out_lots is not None

        with _CONFIG_LOCK:
            # SIZING is refused while a position is open, because
            # manage_position() decides "has the scale-out already fired?" by
            # comparing the position's volume against lot_size. Lower the size
            # mid-trade and an already-reduced position looks untouched, so it is
            # scaled out a second time. The alternative -- remembering the size
            # per ticket -- would have to survive restarts, reconnects and
            # BotManager replacing the thread, which is exactly the state that
            # rule was written to avoid.
            #
            # The refusal is scoped to those two keys, NOT to every edit. That
            # reasoning is lot_size's alone: `exit_at_mean` cannot re-scale
            # anything, and the moment someone reaches for that switch is while a
            # trade is running and the centre line is closing in on it. Refusing
            # it then would withhold the control in the only situation that
            # motivates it.
            open_now = len(bot_positions(symbol))
            if open_now and touches_sizing:
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

            if exit_at_mean is None:
                new_at_mean = bool(cfg.get("exit_at_mean", False))
            else:
                new_at_mean = _validated("exit_at_mean", exit_at_mean)
                if new_at_mean:
                    # Said out loud because the rule is off by default for a
                    # measured reason, and turning it back on is not a cosmetic
                    # choice: the centre line sits inside the target, so the
                    # scaled-out runner will usually be closed before reaching
                    # it. See CLAUDE.md, "Scale-out / break-even".
                    notes.append(
                        "Centre-line exit ON -- a scaled-out runner will usually "
                        "be closed at the centre line instead of the target.")

            # The write happens BEFORE SYMBOL_CONFIG is updated, and is allowed
            # to raise. Postgres now holds the settings, so an in-memory value
            # that the database refused would be traded for the rest of the
            # process and then vanish on restart -- the same silent-restore
            # failure from the other direction. Refuse the edit visibly instead:
            # the caller turns DatabaseUnavailable into a message on the form,
            # exactly as it does ConfigRejected.
            _persist_settings = repo.save_settings(
                symbol, new_lot, new_fraction, new_at_mean, source="api",
                notes=notes or None)
            cfg["lot_size"] = _persist_settings["lot_size"]
            cfg["partial_fraction"] = _persist_settings["partial_fraction"]
            cfg["exit_at_mean"] = _persist_settings["exit_at_mean"]

        log("%s: settings set to lot_size=%g partial_fraction=%.4f (%g lots out) "
            "exit_at_mean=%s"
            % (symbol, new_lot, new_fraction, new_lot * new_fraction, new_at_mean))
        result = self.get_settings(symbol)
        result["notes"] = notes
        return result

    # ---- backtest -----------------------------------------------------------

    def run_backtest(self, symbol, start_date, end_date, initial_balance,
                     lot_size=None, partial_fraction=None):
        """Fetch bars and run the legacy engine for ONE symbol.

        `lot_size` / `partial_fraction` override the live settings for this run
        only -- nothing is written back. The returned dict carries
        `closed_trades`, which run_backtests() needs to merge symbols onto one
        account and then strips; it is not part of what /backtest answers.
        """
        if symbol not in SYMBOL_CONFIG:
            # Not a fallback to gold. The bars would be Bitcoin's and the pip
            # gold's, and the run would report a plausible number for a strategy
            # nobody configured.
            return {"error": "Unknown symbol %r. Configured: %s"
                             % (symbol, ", ".join(SUPPORTED_SYMBOLS))}
        if not mt5.initialize():
            return {"error": "MT5 Init Failed"}

        # Fetch rates
        rates = mt5.copy_rates_range(symbol, TIMEFRAME, start_date, end_date)
        if rates is None or len(rates) == 0:
            return {"error": "No historical data found for %s in the given range"
                             % symbol}

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')

        # Create a temporary bot instance to use its envelope logic
        bot = TradingBot(symbol)
        with _CONFIG_LOCK:
            config = dict(SYMBOL_CONFIG[symbol])
        if lot_size is not None:
            config["lot_size"] = _validated("lot_size", lot_size)
        if partial_fraction is not None:
            config["partial_fraction"] = _validated("partial_fraction", partial_fraction)
        outs, uppers, lowers = bot.calculate_envelope(df)

        vol_min, _vol_max, step, _from_broker = _volume_limits(symbol)
        return simulate_legacy(df, outs, uppers, lowers, config, initial_balance,
                               volume_min=vol_min, volume_step=step)

    def run_backtests(self, symbols, start_date, end_date, initial_balance,
                      sizing=None):
        """Backtest one or several symbols; several are replayed onto ONE account.

        `sizing` is {symbol: {"lot_size": .., "partial_fraction": ..}} -- per
        symbol, because a lot is not a comparable unit across symbols. 0.1 lots
        of gold and 0.1 lots of Bitcoin risk about the same $70 here, but only by
        coincidence of the two contract sizes; one number applied to both would
        be a different bet on each the moment a third symbol arrives.

        The single-symbol answer keeps the shape it has always had, so stored
        runs and anything reading `lot_size` off the top level still work; it
        just gains `symbols` and `per_symbol`. A multi-symbol answer is the
        combined one, with the per-symbol results underneath it.

        A symbol that fails takes the whole run down rather than being dropped:
        "gold and Bitcoin combined" silently answered with gold alone is the kind
        of result someone acts on.
        """
        sizing = sizing or {}
        wanted = []
        for symbol in symbols:
            if symbol not in wanted:
                wanted.append(symbol)
        if not wanted:
            return {"error": "No symbol selected."}

        results = {}
        for symbol in wanted:
            per = sizing.get(symbol) or {}
            res = self.run_backtest(symbol, start_date, end_date, initial_balance,
                                    lot_size=per.get("lot_size"),
                                    partial_fraction=per.get("partial_fraction"))
            if res.get("error"):
                return res
            results[symbol] = res

        if len(wanted) == 1:
            symbol = wanted[0]
            single = {k: v for k, v in results[symbol].items()
                      if k != "closed_trades"}
            out = dict(single)
            out["symbols"] = wanted
            out["combined"] = False
            # Present even for one symbol, so the dashboard has a single shape to
            # render instead of a branch that only the combined case exercises.
            out["per_symbol"] = {symbol: single}
            return out

        return combine_legacy_results(results, initial_balance)

    # ---- account ------------------------------------------------------------

    # How stale a stored account snapshot may be before it is re-captured. The
    # period profits behind it are four history_deals_get() calls over IPC and
    # the dashboard polls every 5 SECONDS, so the un-throttled version ran ~2880
    # year-long history scans an hour. That is the same mistake
    # update_performance_stats() was throttled to once a minute to avoid,
    # repeated in the account panel.
    ACCOUNT_SNAPSHOT_MAX_AGE = float(os.environ.get("BOT_ACCOUNT_SNAPSHOT_SECONDS", "60"))

    def get_account_info(self):
        """The account panel, served from `account_snapshots`.

        A fresh reading is captured at most once a minute; every other call
        reads the stored row back. The rows also accumulate into an equity
        curve, which nothing in the live path recorded before.

        `time_profits` stays ACCOUNT-WIDE -- every deal, any magic -- because
        that is what this panel has always shown. The bot's own figures come
        from `trades` and are its positions only (S1); mixing the two would
        answer neither question.
        """
        try:
            age = repo.account_snapshot_age_seconds()
        except Exception as exc:
            # No stored row to fall back on, so read MT5 directly rather than
            # showing an empty panel.
            log("db: could not read the account snapshot age -- %r" % exc)
            age = None
            captured = self._capture_account_snapshot(persist=False)
            return captured or {}

        if age is None or age >= self.ACCOUNT_SNAPSHOT_MAX_AGE:
            captured = self._capture_account_snapshot(persist=True)
            if captured is not None:
                return captured

        try:
            row = repo.latest_account_snapshot()
        except Exception as exc:
            log("db: could not read the account snapshot -- %r" % exc)
            return {}
        if row is None:
            return {}
        # Reaching here having already passed the throttle means
        # _capture_account_snapshot() returned None -- MT5 is not answering --
        # so this row is as old as the database just said it was. `stale` was
        # hardcoded False, which is how a reading taken at 11:04 was served at
        # 17:53 labelled fresh: the panel prints "refreshed at most once a
        # minute" beside the stamp, so an unflagged old one reads as a clock
        # bug rather than as a terminal that went away. Age comes from the
        # SELECT (`now() - MAX(captured_at)`, one clock) and not from
        # subtracting timestamps here, so it cannot be skewed by this host's.
        stale = age is None or age >= self.ACCOUNT_SNAPSHOT_MAX_AGE
        return {
            "balance": row.get("balance") or 0.0,
            "equity": row.get("equity") or 0.0,
            "profit": row.get("profit") or 0.0,
            "leverage": row.get("leverage") or 0,
            "margin": row.get("margin") or 0.0,
            "drawdown": row.get("drawdown_pct") or 0.0,
            "time_profits": dict(row.get("period_profits") or {}),
            "captured_at": row.get("captured_at"),
            "age_seconds": age,
            "stale": stale,
        }

    def _capture_account_snapshot(self, persist=True):
        """Read MT5 once and (optionally) store the result. None if MT5 is down.

        Returning None rather than zeros matters: a balance of 0 with an equity
        of 0 renders as a real, empty account, while None lets the caller fall
        back to the last stored reading and say when it was taken.
        """
        acc = mt5.account_info()
        if acc is None:
            log("account: account_info() returned None (%s)" % (mt5.last_error(),))
            return None
        acc_dict = acc._asdict() if hasattr(acc, "_asdict") else dict(acc)

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
                # Costs included. The old sum was `d.profit` alone, which reports
                # the gross and calls it profit -- commission and swap are real
                # money and are already signed negative by MT5.
                profits[label] = sum(
                    d.profit + getattr(d, "commission", 0.0) + getattr(d, "swap", 0.0)
                    + getattr(d, "fee", 0.0)
                    for d in history if d.entry == mt5.DEAL_ENTRY_OUT)
            else:
                profits[label] = 0.0

        balance = float(acc_dict.get("balance", 0) or 0)
        equity = float(acc_dict.get("equity", 0) or 0)
        values = {
            "login": acc_dict.get("login"),
            "currency": acc_dict.get("currency"),
            "balance": balance,
            "equity": equity,
            "profit": float(acc_dict.get("profit", 0) or 0),
            "margin": float(acc_dict.get("margin", 0) or 0),
            "margin_free": float(acc_dict.get("margin_free", 0) or 0),
            "leverage": acc_dict.get("leverage"),
            "drawdown_pct": ((balance - equity) / balance * 100.0) if balance else 0.0,
        }

        captured_at = now
        if persist:
            try:
                stored = repo.save_account_snapshot(values, profits)
                captured_at = stored.get("captured_at", now)
            except Exception as exc:
                log("db: could not store the account snapshot -- %r" % exc)

        return {
            "balance": values["balance"],
            "equity": values["equity"],
            "profit": values["profit"],
            "leverage": values["leverage"] or 0,
            "margin": values["margin"],
            "drawdown": values["drawdown_pct"],
            "time_profits": profits,
            "captured_at": captured_at,
            "age_seconds": 0.0,
            "stale": False,
        }

    def equity_curve(self, limit=500):
        """Balance/equity samples, oldest first, for charting."""
        return repo.account_equity_curve(limit=limit)
