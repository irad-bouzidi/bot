"""Every query the dashboard and the live loop make. No SQL anywhere else.

Python 3.8: `typing.Optional/List/Dict` and `# type:` comments, never `X | Y`.

Two rules this module exists to enforce:

  * **The store cannot widen its own reach.** `load_settings()` SELECTs exactly
    the two EDITABLE_KEYS columns for exactly the symbols the caller names, and
    pushes both through the caller's validator. The file-based store had the
    same narrowness written into `_load_settings()`; moving to a database makes
    it matter more, not less, because a database is writable by `psql`, by a
    migration and by anything else with the DSN -- so a row must still not be
    able to introduce a symbol or move a stop.
  * **Aggregates are derived, never accumulated.** Win/loss/P&L counts are
    `SELECT`s over `trades`, which is itself folded from `deals`. Nothing keeps
    a running total in a variable, so a restart, a double-poll or a re-scan of
    an overlapping history window cannot drift the numbers.
"""

import os

from backend.db import pool
from backend.db.pool import cursor, json_param

# Volume comparisons use the same epsilon as the live loop, so "fully closed"
# means the same thing here as it does in manage_position().
_EPS = 1e-9

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

def schema_sql():
    # type: () -> str
    with open(_SCHEMA_PATH) as fh:
        return fh.read()


def apply_schema():
    # type: () -> None
    """Run schema.sql. Idempotent -- safe against a live database."""
    with pool.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(schema_sql())
        finally:
            cur.close()


def schema_version():
    # type: () -> int
    """Highest applied version, or 0 if the schema has never been applied.

    The table's own existence is checked first. Selecting from it and catching
    the error would work, but the failed statement aborts the transaction and
    the connection goes back to a POOL -- see pool._Connection.
    """
    with cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS n FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_name = 'schema_version'
        """)
        if int(cur.fetchone()["n"]) == 0:
            return 0
        cur.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_version")
        return int(cur.fetchone()["version"] or 0)


def tables_present():
    # type: () -> bool
    with cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS n FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name IN ('symbol_settings', 'trades', 'deals',
                                 'bot_state', 'backtest_runs', 'ui_preferences')
        """)
        return int(cur.fetchone()["n"]) == 6


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------

def load_settings(symbols, validate=None, on_reject=None):
    """The persisted sizing for `symbols`, as {symbol: {key: value}}.

    Only the two editable columns, only the symbols asked for, and every value
    passed through `validate(key, value)` before it is handed back. A value the
    validator refuses is dropped and reported through `on_reject` rather than
    returned, so an out-of-range row leaves the code default standing -- the
    same contract the JSON loader had, for the same reason: this is the number
    that decides how much money is at risk per trade.
    """
    if not symbols:
        return {}
    out = {}
    with cursor() as cur:
        cur.execute("""
            SELECT symbol, lot_size, partial_fraction
            FROM symbol_settings
            WHERE symbol = ANY(%s)
        """, (list(symbols),))
        rows = cur.fetchall()
    for row in rows:
        symbol = row["symbol"]
        values = {}
        for key in ("lot_size", "partial_fraction"):
            raw = row[key]
            if raw is None:
                continue
            if validate is None:
                values[key] = float(raw)
                continue
            try:
                values[key] = validate(key, raw)
            except Exception as exc:
                if on_reject is not None:
                    on_reject(symbol, key, exc)
        if values:
            out[symbol] = values
    return out


def save_settings(symbol, lot_size, partial_fraction, source="api", notes=None):
    """Upsert the sizing and append an audit row, in ONE transaction.

    Together, because the audit row's `prev_*` columns are read from the table
    in the same statement that overwrites it. Split across two transactions, a
    concurrent save would let both audit rows claim the same previous value and
    the history would no longer reconstruct.
    """
    note = None if not notes else " ".join(notes) if isinstance(notes, (list, tuple)) else str(notes)
    with cursor() as cur:
        cur.execute("""
            WITH prev AS (
                SELECT lot_size, partial_fraction
                FROM symbol_settings WHERE symbol = %(symbol)s
            ), upsert AS (
                INSERT INTO symbol_settings (symbol, lot_size, partial_fraction, updated_at)
                VALUES (%(symbol)s, %(lot)s, %(fraction)s, now())
                ON CONFLICT (symbol) DO UPDATE
                    SET lot_size = EXCLUDED.lot_size,
                        partial_fraction = EXCLUDED.partial_fraction,
                        updated_at = now()
                RETURNING lot_size, partial_fraction, updated_at
            )
            INSERT INTO settings_audit
                (symbol, lot_size, partial_fraction,
                 prev_lot_size, prev_partial_fraction, source, notes)
            SELECT %(symbol)s, u.lot_size, u.partial_fraction,
                   (SELECT lot_size FROM prev), (SELECT partial_fraction FROM prev),
                   %(source)s, %(notes)s
            FROM upsert u
            RETURNING lot_size, partial_fraction
        """, {"symbol": symbol, "lot": float(lot_size),
              "fraction": float(partial_fraction), "source": source, "notes": note})
        row = cur.fetchone()
        return {"lot_size": float(row["lot_size"]),
                "partial_fraction": float(row["partial_fraction"])}


def settings_history(symbol=None, limit=50):
    with cursor() as cur:
        cur.execute("""
            SELECT id, symbol, lot_size, partial_fraction,
                   prev_lot_size, prev_partial_fraction, source, notes, created_at
            FROM settings_audit
            WHERE (%(symbol)s IS NULL OR symbol = %(symbol)s)
            ORDER BY created_at DESC, id DESC
            LIMIT %(limit)s
        """, {"symbol": symbol, "limit": int(limit)})
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# bot run state
# ---------------------------------------------------------------------------

def ensure_bot_rows(symbols):
    """One `bot_state` and one `bot_snapshots` row per configured symbol.

    Called at boot so every later write is a plain UPDATE and `/stats` never has
    to distinguish "no row yet" from "stopped".
    """
    if not symbols:
        return
    with cursor() as cur:
        for symbol in symbols:
            cur.execute("""
                INSERT INTO bot_state (symbol) VALUES (%s)
                ON CONFLICT (symbol) DO NOTHING
            """, (symbol,))
            cur.execute("""
                INSERT INTO bot_snapshots (symbol) VALUES (%s)
                ON CONFLICT (symbol) DO NOTHING
            """, (symbol,))


def get_bot_state(symbol):
    with cursor() as cur:
        cur.execute("""
            SELECT symbol, desired_state, last_bar_time, last_entry_bar,
                   last_error, started_at, stopped_at, updated_at
            FROM bot_state WHERE symbol = %s
        """, (symbol,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_bot_states():
    with cursor() as cur:
        cur.execute("""
            SELECT symbol, desired_state, last_bar_time, last_entry_bar,
                   last_error, started_at, stopped_at, updated_at
            FROM bot_state
        """)
        return dict((r["symbol"], dict(r)) for r in cur.fetchall())


def set_desired_state(symbol, desired_state):
    """Record what the user asked for. NOT what the thread is doing.

    Reported alongside the live status rather than instead of it: they can
    legitimately disagree (the process restarted, the thread crashed), and
    collapsing them into one word is how a dead bot comes to report "Running".
    """
    if desired_state not in ("running", "stopped"):
        raise ValueError("desired_state must be 'running' or 'stopped'")
    with cursor() as cur:
        cur.execute("""
            INSERT INTO bot_state (symbol, desired_state, started_at, stopped_at, updated_at)
            VALUES (%(symbol)s, %(state)s,
                    CASE WHEN %(state)s = 'running' THEN now() END,
                    CASE WHEN %(state)s = 'stopped' THEN now() END,
                    now())
            ON CONFLICT (symbol) DO UPDATE SET
                desired_state = EXCLUDED.desired_state,
                started_at = CASE WHEN EXCLUDED.desired_state = 'running'
                                  THEN now() ELSE bot_state.started_at END,
                stopped_at = CASE WHEN EXCLUDED.desired_state = 'stopped'
                                  THEN now() ELSE bot_state.stopped_at END,
                updated_at = now()
        """, {"symbol": symbol, "state": desired_state})


def get_bar_marks(symbol):
    """(last_bar_time, last_entry_bar) -- the S4 and COOLDOWN_BARS guards.

    These were instance attributes on TradingBot, so Stop-then-Start reset them
    to None and the bot could enter again on the very bar it had just entered
    on. That is the repeat-fire S4 exists to prevent, reachable from the
    dashboard's own buttons.
    """
    row = get_bot_state(symbol)
    if row is None:
        return None, None
    return row["last_bar_time"], row["last_entry_bar"]


def set_bar_marks(symbol, last_bar_time=None, last_entry_bar=None):
    """Persist either bar mark. COALESCE, so passing one never clears the other."""
    with cursor() as cur:
        cur.execute("""
            INSERT INTO bot_state (symbol, last_bar_time, last_entry_bar, updated_at)
            VALUES (%(symbol)s, %(bar)s, %(entry)s, now())
            ON CONFLICT (symbol) DO UPDATE SET
                last_bar_time = COALESCE(EXCLUDED.last_bar_time, bot_state.last_bar_time),
                last_entry_bar = COALESCE(EXCLUDED.last_entry_bar, bot_state.last_entry_bar),
                updated_at = now()
        """, {"symbol": symbol, "bar": last_bar_time, "entry": last_entry_bar})


def set_bot_error(symbol, message):
    with cursor() as cur:
        cur.execute("""
            INSERT INTO bot_state (symbol, last_error, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (symbol) DO UPDATE SET
                last_error = EXCLUDED.last_error, updated_at = now()
        """, (symbol, message))


def record_control_event(symbol, action, accepted, detail=None):
    """Append-only log of every start/stop press, accepted or refused.

    POST /control starts live trading with real money and has no
    authentication (CLAUDE.md, Safety). An append-only record of what it was
    asked to do is the cheapest accountability available.
    """
    with cursor() as cur:
        cur.execute("""
            INSERT INTO control_events (symbol, action, accepted, detail)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (symbol, action, bool(accepted), detail))
        return int(cur.fetchone()["id"])


def list_control_events(symbol=None, limit=50):
    with cursor() as cur:
        cur.execute("""
            SELECT id, symbol, action, accepted, detail, created_at
            FROM control_events
            WHERE (%(symbol)s IS NULL OR symbol = %(symbol)s)
            ORDER BY created_at DESC, id DESC
            LIMIT %(limit)s
        """, {"symbol": symbol, "limit": int(limit)})
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# live indicator snapshot
# ---------------------------------------------------------------------------

def save_snapshot(symbol, status, last_close=None, nw_out=None, nw_upper=None,
                  nw_lower=None, bar_time=None, open_positions=0, detail=None):
    """Overwrite this symbol's latest reading.

    `updated_at` moves with it, which is the point: the in-memory dict served
    its final values forever with no way to tell a live reading from one frozen
    at the moment the thread died.
    """
    with cursor() as cur:
        cur.execute("""
            INSERT INTO bot_snapshots
                (symbol, status, last_close, nw_out, nw_upper, nw_lower,
                 bar_time, open_positions, detail, updated_at)
            VALUES (%(symbol)s, %(status)s, %(close)s, %(out)s, %(upper)s,
                    %(lower)s, %(bar)s, %(positions)s, %(detail)s, now())
            ON CONFLICT (symbol) DO UPDATE SET
                status = EXCLUDED.status,
                last_close = COALESCE(EXCLUDED.last_close, bot_snapshots.last_close),
                nw_out = COALESCE(EXCLUDED.nw_out, bot_snapshots.nw_out),
                nw_upper = COALESCE(EXCLUDED.nw_upper, bot_snapshots.nw_upper),
                nw_lower = COALESCE(EXCLUDED.nw_lower, bot_snapshots.nw_lower),
                bar_time = COALESCE(EXCLUDED.bar_time, bot_snapshots.bar_time),
                open_positions = EXCLUDED.open_positions,
                detail = EXCLUDED.detail,
                updated_at = now()
        """, {"symbol": symbol, "status": status, "close": last_close,
              "out": nw_out, "upper": nw_upper, "lower": nw_lower,
              "bar": bar_time, "positions": int(open_positions), "detail": detail})


def set_snapshot_status(symbol, status, detail=None):
    """Move only the status, leaving the last envelope reading in place.

    A stopped bot still shows where the bands were, instead of the zeros the
    old `{"status": "Stopped"}` fallback produced.
    """
    with cursor() as cur:
        cur.execute("""
            INSERT INTO bot_snapshots (symbol, status, detail, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (symbol) DO UPDATE SET
                status = EXCLUDED.status, detail = EXCLUDED.detail, updated_at = now()
        """, (symbol, status, detail))


def get_snapshots():
    with cursor() as cur:
        cur.execute("""
            SELECT symbol, status, last_close, nw_out, nw_upper, nw_lower,
                   bar_time, open_positions, detail, updated_at
            FROM bot_snapshots
        """)
        return dict((r["symbol"], dict(r)) for r in cur.fetchall())


# ---------------------------------------------------------------------------
# deals -> trades
# ---------------------------------------------------------------------------

DEAL_COLUMNS = ("ticket", "order_ticket", "position_id", "symbol", "magic",
                "entry_kind", "deal_type", "volume", "price", "profit",
                "commission", "swap", "fee", "comment", "dealt_at")


def upsert_deals(rows):
    """Insert or refresh raw MT5 deals. Keyed by the broker's deal ticket.

    Idempotent by construction, which is what lets the reconcile pass re-scan
    an overlapping window every minute -- and re-scan a full 365 days after a
    restart -- without double-counting. `swap` in particular is credited to a
    deal after the fact, so re-reading an old deal has to be able to correct the
    stored row rather than being rejected as a duplicate.
    """
    rows = [r for r in rows if r.get("ticket") is not None]
    if not rows:
        return 0
    psycopg2 = pool._driver()
    values = [tuple(r.get(col) for col in DEAL_COLUMNS) for r in rows]
    sql = """
        INSERT INTO deals (%s) VALUES %%s
        ON CONFLICT (ticket) DO UPDATE SET
            order_ticket = EXCLUDED.order_ticket,
            position_id  = EXCLUDED.position_id,
            symbol       = EXCLUDED.symbol,
            magic        = EXCLUDED.magic,
            entry_kind   = EXCLUDED.entry_kind,
            deal_type    = EXCLUDED.deal_type,
            volume       = EXCLUDED.volume,
            price        = EXCLUDED.price,
            profit       = EXCLUDED.profit,
            commission   = EXCLUDED.commission,
            swap         = EXCLUDED.swap,
            fee          = EXCLUDED.fee,
            comment      = EXCLUDED.comment,
            dealt_at     = EXCLUDED.dealt_at
    """ % ", ".join(DEAL_COLUMNS)
    with pool.connection() as conn:
        cur = conn.cursor()
        try:
            psycopg2.extras.execute_values(cur, sql, values, page_size=200)
        finally:
            cur.close()
    return len(rows)


# Folds `deals` into one row per POSITION.
#
# Grouping by position rather than by closing deal is a behaviour change, not
# just a schema one. update_performance_stats() counted every DEAL_ENTRY_OUT
# separately, so a trade that scaled out and then stopped at break-even booked
# one win plus one flat -- two rows for one trade, the win rate lifted by the
# scale-out and no loss recorded against it. The scale-out is measured NEGATIVE
# for expectancy on cached gold data (CLAUDE.md), so that arithmetic flattered
# precisely the rule the data says is losing money.
#
# `side` comes from the ENTRY deal's type, never from the exit: the exit deal of
# a long is a sell, so reading the type off any exit would invert every trade.
_REBUILD_TRADES = """
WITH scoped AS (
    SELECT * FROM deals
    WHERE symbol = %(symbol)s
      AND position_id IS NOT NULL
), folded AS (
    SELECT
        position_id,
        MIN(symbol) AS symbol,
        MAX(magic) AS magic,
        -- One entry deal per position for this bot (it never adds to a
        -- position), but MIN(dealt_at, ticket) keeps it deterministic anyway.
        (ARRAY_AGG(deal_type ORDER BY dealt_at, ticket)
            FILTER (WHERE entry_kind = 'in'))[1] AS entry_type,
        MIN(dealt_at) FILTER (WHERE entry_kind = 'in') AS opened_at,
        MAX(dealt_at) FILTER (WHERE entry_kind IN ('out', 'out_by')) AS last_exit_at,
        COALESCE(SUM(volume) FILTER (WHERE entry_kind = 'in'), 0) AS volume_in,
        COALESCE(SUM(volume) FILTER (WHERE entry_kind IN ('out', 'out_by')), 0) AS volume_out,
        COUNT(*) FILTER (WHERE entry_kind IN ('out', 'out_by')) AS exit_count,
        -- Volume-weighted, so a scaled-out trade reports the average price it
        -- actually left at rather than whichever leg happened to be last.
        CASE WHEN COALESCE(SUM(volume) FILTER (WHERE entry_kind = 'in'), 0) > 0
             THEN SUM(price * volume) FILTER (WHERE entry_kind = 'in')
                  / SUM(volume) FILTER (WHERE entry_kind = 'in') END AS entry_price,
        CASE WHEN COALESCE(SUM(volume) FILTER (WHERE entry_kind IN ('out', 'out_by')), 0) > 0
             THEN SUM(price * volume) FILTER (WHERE entry_kind IN ('out', 'out_by'))
                  / SUM(volume) FILTER (WHERE entry_kind IN ('out', 'out_by')) END AS exit_price,
        COALESCE(SUM(profit), 0) AS gross_profit,
        COALESCE(SUM(commission), 0) AS commission,
        COALESCE(SUM(swap), 0) AS swap,
        COALESCE(SUM(fee), 0) AS fee,
        (ARRAY_AGG(comment ORDER BY dealt_at DESC, ticket DESC)
            FILTER (WHERE comment IS NOT NULL AND comment <> ''))[1] AS comment
    FROM scoped
    GROUP BY position_id
)
INSERT INTO trades (
    position_id, symbol, magic, side, status, opened_at, closed_at,
    entry_price, exit_price, volume_in, volume_out, exit_count,
    gross_profit, commission, swap, fee, net_profit, comment, updated_at)
SELECT
    position_id, symbol, magic,
    CASE WHEN entry_type = 'buy' THEN 'long' ELSE 'short' END,
    CASE WHEN volume_in > 0 AND volume_out >= volume_in - %(eps)s
         THEN 'closed' ELSE 'open' END,
    opened_at,
    -- NULL while the position is still open, even when part of it has been
    -- banked: a scale-out is not a close, and dating the trade by its partial
    -- exit would drop it into a closed-trade equity curve early.
    CASE WHEN volume_in > 0 AND volume_out >= volume_in - %(eps)s
         THEN last_exit_at END,
    entry_price, exit_price, volume_in, volume_out, exit_count,
    gross_profit, commission, swap, fee,
    -- MT5 reports commission/swap/fee already signed (negative when charged),
    -- so this is a sum and not a subtraction. Getting that backwards turns
    -- every cost into a credit.
    gross_profit + commission + swap + fee,
    comment, now()
FROM folded
WHERE opened_at IS NOT NULL
ON CONFLICT (position_id) DO UPDATE SET
    symbol = EXCLUDED.symbol,
    magic = EXCLUDED.magic,
    side = EXCLUDED.side,
    status = EXCLUDED.status,
    opened_at = EXCLUDED.opened_at,
    closed_at = EXCLUDED.closed_at,
    entry_price = EXCLUDED.entry_price,
    exit_price = EXCLUDED.exit_price,
    volume_in = EXCLUDED.volume_in,
    volume_out = EXCLUDED.volume_out,
    exit_count = EXCLUDED.exit_count,
    gross_profit = EXCLUDED.gross_profit,
    commission = EXCLUDED.commission,
    swap = EXCLUDED.swap,
    fee = EXCLUDED.fee,
    net_profit = EXCLUDED.net_profit,
    comment = EXCLUDED.comment,
    updated_at = now()
"""


def rebuild_trades(symbol):
    """Re-fold ALL of `deals` into `trades` for one symbol. Returns rows written.

    Derived, never accumulated: re-running it over the same deals produces the
    same trades, so the aggregate cannot drift away from the raw record no
    matter how the reconcile window overlaps.

    There is deliberately **no date window** here, tempting as one is on a
    growing table. A position's deals can straddle any cut-off: fold only the
    recent ones and a trade whose entry fell before the boundary arrives with no
    entry deal, so it has no side and no entry price, gets skipped by the
    `opened_at IS NOT NULL` guard, and its row stays `open` forever with the
    loss never counted. The scan is a grouped index scan over one symbol's
    deals -- a few thousand rows after years of trading -- so the whole-table
    fold is the cheap option as well as the correct one.
    """
    with pool.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(_REBUILD_TRADES, {"symbol": symbol, "eps": _EPS})
            return cur.rowcount
        finally:
            cur.close()


def latest_deal_time(symbol):
    """Newest stored deal for `symbol`, so a reconcile can scan from there.

    Returned as-is (may be None on an empty table); the caller decides the
    overlap to re-scan, because `swap` and `commission` can still change on a
    deal after it is first seen.
    """
    with cursor() as cur:
        cur.execute("SELECT MAX(dealt_at) AS t FROM deals WHERE symbol = %s",
                    (symbol,))
        row = cur.fetchone()
        return row["t"] if row else None


# ---------------------------------------------------------------------------
# trade reporting
# ---------------------------------------------------------------------------

def trade_stats(symbol):
    """Wins, losses, P&L and realised drawdown, computed from `trades`.

    One trade is one outcome, decided on NET profit -- costs included. A trade
    that banked a scale-out and then stopped at break-even is one trade, and it
    is a win only if what it kept exceeds what the costs took.

    `max_drawdown` is the deepest peak-to-trough of the CLOSED-trade equity
    curve, in account currency. It is not the intrabar drawdown the research
    engine reports (that comes from the equity curve including open positions,
    which nothing samples here), and it is not a percentage: the live account
    balance moves for deposits too, so dividing by it would make the figure
    move without a trade happening. The live stat it replaces was initialised
    to 0.0 and never written at all.
    """
    with cursor() as cur:
        cur.execute("""
            WITH scoped AS (
                SELECT * FROM trades WHERE symbol = %(symbol)s
            ), curve AS (
                SELECT closed_at, position_id,
                       SUM(net_profit) OVER (ORDER BY closed_at, position_id) AS cum
                FROM scoped WHERE status = 'closed'
            ), peaked AS (
                -- The running peak must advance in the SAME order the curve
                -- accumulates in. Ordering this window by `cum` instead would
                -- make the peak the largest value *so far in value order* --
                -- which is trivially the current row, giving a drawdown of 0
                -- on every input.
                SELECT cum, GREATEST(MAX(cum) OVER (
                           ORDER BY closed_at, position_id
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ), 0) AS peak
                FROM curve
            )
            SELECT
                (SELECT COUNT(*) FROM scoped) AS trades_total,
                (SELECT COUNT(*) FROM scoped WHERE status = 'open') AS trades_open,
                (SELECT COUNT(*) FROM scoped WHERE status = 'closed') AS trades_closed,
                (SELECT COUNT(*) FROM scoped WHERE status = 'closed' AND net_profit > 0) AS wins,
                (SELECT COUNT(*) FROM scoped WHERE status = 'closed' AND net_profit < 0) AS losses,
                (SELECT COUNT(*) FROM scoped WHERE status = 'closed' AND net_profit = 0) AS breakeven,
                (SELECT COUNT(*) FROM scoped WHERE exit_count > 1) AS scaled_out,
                (SELECT COALESCE(SUM(net_profit), 0) FROM scoped WHERE status = 'closed') AS total_pl,
                (SELECT COALESCE(SUM(gross_profit), 0) FROM scoped WHERE status = 'closed') AS gross_pl,
                (SELECT COALESCE(SUM(commission + swap + fee), 0) FROM scoped WHERE status = 'closed') AS costs,
                (SELECT COALESCE(AVG(net_profit), 0) FROM scoped WHERE status = 'closed' AND net_profit > 0) AS avg_win,
                (SELECT COALESCE(AVG(net_profit), 0) FROM scoped WHERE status = 'closed' AND net_profit < 0) AS avg_loss,
                (SELECT COALESCE(MAX(peak - cum), 0) FROM peaked) AS max_drawdown,
                (SELECT MAX(closed_at) FROM scoped WHERE status = 'closed') AS last_closed_at
        """, {"symbol": symbol})
        row = dict(cur.fetchone())

    for key in ("trades_total", "trades_open", "trades_closed", "wins",
                "losses", "breakeven", "scaled_out"):
        row[key] = int(row[key] or 0)
    for key in ("total_pl", "gross_pl", "costs", "avg_win", "avg_loss",
                "max_drawdown"):
        row[key] = float(row[key] or 0.0)

    decided = row["wins"] + row["losses"]
    # Break-even trades are excluded from the denominator rather than counted as
    # losses: with the stop moved to entry they are a designed outcome of the
    # scale-out rule, and either treatment misreports it if left implicit --
    # so `breakeven` is returned beside the rate.
    row["win_rate"] = (row["wins"] / float(decided) * 100.0) if decided else 0.0
    return row


def list_trades(symbol=None, status=None, limit=200, offset=0):
    with cursor() as cur:
        cur.execute("""
            SELECT position_id, symbol, magic, side, status, opened_at, closed_at,
                   entry_price, exit_price, volume_in, volume_out, exit_count,
                   gross_profit, commission, swap, fee, net_profit, comment,
                   updated_at
            FROM trades
            WHERE (%(symbol)s IS NULL OR symbol = %(symbol)s)
              AND (%(status)s IS NULL OR status = %(status)s)
            ORDER BY opened_at DESC, position_id DESC
            LIMIT %(limit)s OFFSET %(offset)s
        """, {"symbol": symbol, "status": status,
              "limit": int(limit), "offset": int(offset)})
        return [dict(r) for r in cur.fetchall()]


def count_trades(symbol=None, status=None):
    with cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS n FROM trades
            WHERE (%(symbol)s IS NULL OR symbol = %(symbol)s)
              AND (%(status)s IS NULL OR status = %(status)s)
        """, {"symbol": symbol, "status": status})
        return int(cur.fetchone()["n"])


def list_deals(position_id):
    with cursor() as cur:
        cur.execute("""
            SELECT ticket, order_ticket, position_id, symbol, magic, entry_kind,
                   deal_type, volume, price, profit, commission, swap, fee,
                   comment, dealt_at
            FROM deals WHERE position_id = %s
            ORDER BY dealt_at, ticket
        """, (int(position_id),))
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# account snapshots
# ---------------------------------------------------------------------------

def save_account_snapshot(values, period_profits):
    with cursor() as cur:
        cur.execute("""
            INSERT INTO account_snapshots
                (login, currency, balance, equity, profit, margin, margin_free,
                 leverage, drawdown_pct, period_profits)
            VALUES (%(login)s, %(currency)s, %(balance)s, %(equity)s, %(profit)s,
                    %(margin)s, %(margin_free)s, %(leverage)s, %(drawdown_pct)s,
                    %(period_profits)s)
            RETURNING id, captured_at
        """, {
            "login": values.get("login"),
            "currency": values.get("currency"),
            "balance": values.get("balance"),
            "equity": values.get("equity"),
            "profit": values.get("profit"),
            "margin": values.get("margin"),
            "margin_free": values.get("margin_free"),
            "leverage": values.get("leverage"),
            "drawdown_pct": values.get("drawdown_pct"),
            "period_profits": json_param(period_profits or {}),
        })
        return dict(cur.fetchone())


def latest_account_snapshot():
    with cursor() as cur:
        cur.execute("""
            SELECT id, login, currency, balance, equity, profit, margin,
                   margin_free, leverage, drawdown_pct, period_profits, captured_at
            FROM account_snapshots ORDER BY captured_at DESC, id DESC LIMIT 1
        """)
        row = cur.fetchone()
        return dict(row) if row else None


def account_snapshot_age_seconds():
    """Seconds since the newest snapshot, or None if there is none.

    The throttle reads from this rather than from a module-level timestamp so
    that a restarted process does not immediately re-run the four 365-day
    history scans the snapshot exists to avoid.
    """
    with cursor() as cur:
        cur.execute("""
            SELECT EXTRACT(EPOCH FROM (now() - MAX(captured_at))) AS age
            FROM account_snapshots
        """)
        row = cur.fetchone()
        return float(row["age"]) if row and row["age"] is not None else None


def account_equity_curve(limit=500):
    with cursor() as cur:
        cur.execute("""
            SELECT captured_at, balance, equity FROM (
                SELECT captured_at, balance, equity
                FROM account_snapshots
                ORDER BY captured_at DESC, id DESC
                LIMIT %s
            ) recent ORDER BY captured_at
        """, (int(limit),))
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# backtest runs
# ---------------------------------------------------------------------------

def record_backtest(symbol, start_date, end_date, initial_balance,
                    lot_size=None, scale_out_lots=None, partial_fraction=None,
                    engine="legacy", status="ok", error=None, result=None,
                    duration_ms=None):
    """Store one run, inputs and outputs together.

    Errored runs are stored too. A run that found no bars for its window is a
    fact about that window, and discarding it is how the same unavailable range
    gets asked for five times.
    """
    with cursor() as cur:
        cur.execute("""
            INSERT INTO backtest_runs
                (engine, symbol, start_date, end_date, initial_balance, lot_size,
                 scale_out_lots, partial_fraction, status, error, result, duration_ms)
            VALUES (%(engine)s, %(symbol)s, %(start)s, %(end)s, %(balance)s,
                    %(lot)s, %(scale_out)s, %(fraction)s, %(status)s, %(error)s,
                    %(result)s, %(duration)s)
            RETURNING id, created_at
        """, {"engine": engine, "symbol": symbol, "start": start_date,
              "end": end_date, "balance": float(initial_balance),
              "lot": lot_size, "scale_out": scale_out_lots,
              "fraction": partial_fraction, "status": status, "error": error,
              "result": json_param(result) if result is not None else None,
              "duration": duration_ms})
        return dict(cur.fetchone())


def list_backtests(symbol=None, limit=25):
    with cursor() as cur:
        cur.execute("""
            SELECT id, engine, symbol, start_date, end_date, initial_balance,
                   lot_size, scale_out_lots, partial_fraction, status, error,
                   result, duration_ms, created_at
            FROM backtest_runs
            WHERE (%(symbol)s IS NULL OR symbol = %(symbol)s)
            ORDER BY created_at DESC, id DESC
            LIMIT %(limit)s
        """, {"symbol": symbol, "limit": int(limit)})
        return [dict(r) for r in cur.fetchall()]


def get_backtest(run_id):
    with cursor() as cur:
        cur.execute("""
            SELECT id, engine, symbol, start_date, end_date, initial_balance,
                   lot_size, scale_out_lots, partial_fraction, status, error,
                   result, duration_ms, created_at
            FROM backtest_runs WHERE id = %s
        """, (int(run_id),))
        row = cur.fetchone()
        return dict(row) if row else None


def delete_backtest(run_id):
    with cursor() as cur:
        cur.execute("DELETE FROM backtest_runs WHERE id = %s", (int(run_id),))
        return cur.rowcount


# ---------------------------------------------------------------------------
# UI preferences
# ---------------------------------------------------------------------------

DEFAULT_SCOPE = "default"


def get_preferences(scope=DEFAULT_SCOPE):
    with cursor() as cur:
        cur.execute("SELECT data FROM ui_preferences WHERE scope = %s", (scope,))
        row = cur.fetchone()
        return dict(row["data"]) if row and row["data"] else {}


def save_preferences(patch, scope=DEFAULT_SCOPE):
    """Shallow-MERGE `patch` into the stored preferences and return the result.

    A merge, not a replace: the theme switch and the backtest form both write
    here, and a replace would mean whichever fired last erased the other's
    field. `||` is jsonb concatenation, right-hand side wins.
    """
    if not isinstance(patch, dict):
        raise ValueError("preferences patch must be an object")
    with cursor() as cur:
        cur.execute("""
            INSERT INTO ui_preferences (scope, data, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (scope) DO UPDATE SET
                data = ui_preferences.data || EXCLUDED.data,
                updated_at = now()
            RETURNING data
        """, (scope, json_param(patch)))
        return dict(cur.fetchone()["data"])


def replace_preferences(data, scope=DEFAULT_SCOPE):
    """Overwrite the whole document. Used by the Reset button, not by edits."""
    if not isinstance(data, dict):
        raise ValueError("preferences must be an object")
    with cursor() as cur:
        cur.execute("""
            INSERT INTO ui_preferences (scope, data, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (scope) DO UPDATE SET
                data = EXCLUDED.data, updated_at = now()
            RETURNING data
        """, (scope, json_param(data)))
        return dict(cur.fetchone()["data"])
