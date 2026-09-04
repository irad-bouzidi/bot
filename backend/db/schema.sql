-- Schema for the trading dashboard's persisted state.
--
-- Applied two ways, and both must stay equivalent:
--   * `python -m backend.db.migrate` against any database, at any time;
--   * /docker-entrypoint-initdb.d/ on the db container's FIRST boot only, where
--     docker-compose.yml mounts THIS FILE (Postgres ignores
--     that directory once a data volume exists), which is why every statement
--     below is IF NOT EXISTS / idempotent rather than a one-shot script.
--
-- What is deliberately NOT here: bars. The bar cache stays `data/*.csv.gz`
-- because pyarrow is unavailable on the pinned Python 3.8 and because the
-- research path must keep running with no database at all.

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- 1. Sizing -- the two runtime-editable numbers, previously data/settings.json
-- ---------------------------------------------------------------------------
-- Only `lot_size` and `partial_fraction` live here, matching EDITABLE_KEYS. The
-- store must never be able to introduce a symbol or move a stop: those come
-- from SYMBOL_CONFIG in code, backtested, and a row here cannot reach them.
--
-- The CHECK constraints are a second line of defence behind _validated(). The
-- file-based store could only reject a bad value on the way *out* (at load),
-- so a hand-edited file kept its bad value silently until the next restart;
-- these reject it on the way *in*.
CREATE TABLE IF NOT EXISTS symbol_settings (
    symbol            TEXT PRIMARY KEY,
    lot_size          DOUBLE PRECISION NOT NULL,
    partial_fraction  DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT symbol_settings_lot_positive
        CHECK (lot_size > 0),
    CONSTRAINT symbol_settings_fraction_range
        CHECK (partial_fraction >= 0 AND partial_fraction < 1)
);

-- Append-only history of every sizing change. `lot_size` is the only risk
-- control this bot has, so "who moved it to 0.5 and when" is worth more than
-- the disk it costs; the JSON file overwrote its own history on every save.
CREATE TABLE IF NOT EXISTS settings_audit (
    id                     BIGSERIAL PRIMARY KEY,
    symbol                 TEXT NOT NULL,
    lot_size               DOUBLE PRECISION NOT NULL,
    partial_fraction       DOUBLE PRECISION NOT NULL,
    prev_lot_size          DOUBLE PRECISION,
    prev_partial_fraction  DOUBLE PRECISION,
    source                 TEXT NOT NULL DEFAULT 'api',
    notes                  TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS settings_audit_symbol_idx
    ON settings_audit (symbol, created_at DESC);


-- ---------------------------------------------------------------------------
-- 2. Bot run state
-- ---------------------------------------------------------------------------
-- `desired_state` is what the user last asked for via POST /control; the thread
-- being alive is the actual state. They are reported separately because they
-- can legitimately disagree (process restarted, thread crashed), and collapsing
-- them into one word is how a dead bot comes to report "Running".
--
-- `last_bar_time` / `last_entry_bar` are the S4 once-per-closed-bar and
-- COOLDOWN_BARS guards. They were instance attributes, so a restart reset them
-- to None and the bot could re-enter on the very bar it had just entered on --
-- exactly the repeat-fire S4 was written to stop, reachable through the front
-- door by pressing Stop then Start.
CREATE TABLE IF NOT EXISTS bot_state (
    symbol           TEXT PRIMARY KEY,
    desired_state    TEXT NOT NULL DEFAULT 'stopped',
    last_bar_time    BIGINT,
    last_entry_bar   BIGINT,
    last_error       TEXT,
    started_at       TIMESTAMPTZ,
    stopped_at       TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT bot_state_desired_state
        CHECK (desired_state IN ('running', 'stopped'))
);

-- Every start/stop press, accepted or refused. POST /control has no
-- authentication (see the Safety section of CLAUDE.md); an append-only record of
-- what it was asked to do is the cheapest accountability available.
CREATE TABLE IF NOT EXISTS control_events (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT NOT NULL,
    action      TEXT NOT NULL,
    accepted    BOOLEAN NOT NULL,
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS control_events_created_idx
    ON control_events (created_at DESC);


-- ---------------------------------------------------------------------------
-- 3. Live indicator snapshot -- previously TradingBot.stats, in memory
-- ---------------------------------------------------------------------------
-- One row per symbol, overwritten each ~15s cycle. `updated_at` is the point:
-- the old in-memory dict served its last values forever with no way to tell a
-- fresh reading from one frozen at the moment the thread died, and a stopped
-- bot's card showed zeros rather than where the envelope actually was.
CREATE TABLE IF NOT EXISTS bot_snapshots (
    symbol          TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'Stopped',
    last_close      DOUBLE PRECISION,
    nw_out          DOUBLE PRECISION,
    nw_upper        DOUBLE PRECISION,
    nw_lower        DOUBLE PRECISION,
    bar_time        BIGINT,
    open_positions  INTEGER NOT NULL DEFAULT 0,
    detail          TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- 4. Trade history
-- ---------------------------------------------------------------------------
-- `deals` is the raw MT5 deal stream, keyed by the broker's own deal ticket so
-- the reconcile pass is an idempotent upsert: it can re-scan an overlapping
-- window every minute, or re-scan 365 days after a restart, without
-- double-counting. Nothing is ever deleted from here -- "never hide losing
-- trades" (Trading Bot.md) means the raw record outlives any aggregate.
CREATE TABLE IF NOT EXISTS deals (
    ticket        BIGINT PRIMARY KEY,
    order_ticket  BIGINT,
    position_id   BIGINT,
    symbol        TEXT NOT NULL,
    magic         BIGINT,
    entry_kind    TEXT NOT NULL,
    deal_type     TEXT NOT NULL,
    volume        DOUBLE PRECISION NOT NULL DEFAULT 0,
    price         DOUBLE PRECISION NOT NULL DEFAULT 0,
    profit        DOUBLE PRECISION NOT NULL DEFAULT 0,
    commission    DOUBLE PRECISION NOT NULL DEFAULT 0,
    swap          DOUBLE PRECISION NOT NULL DEFAULT 0,
    fee           DOUBLE PRECISION NOT NULL DEFAULT 0,
    comment       TEXT,
    dealt_at      TIMESTAMPTZ NOT NULL,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS deals_position_idx ON deals (position_id);
CREATE INDEX IF NOT EXISTS deals_symbol_time_idx ON deals (symbol, dealt_at DESC);

-- One row per POSITION, folded from the deals above.
--
-- Grouping by position rather than by closing deal is a behaviour change, not
-- just a schema one. The old update_performance_stats() counted every
-- DEAL_ENTRY_OUT separately, so a trade that scaled out and then stopped at
-- break-even booked one win plus one flat -- two entries for one trade, with
-- the win rate lifted by the scale-out and no loss recorded against it. The
-- scale-out is measured NEGATIVE for expectancy on cached gold data (CLAUDE.md),
-- so that arithmetic flattered exactly the rule the data says is costing money.
-- `exit_count > 1` is what a scaled-out trade looks like here.
CREATE TABLE IF NOT EXISTS trades (
    position_id    BIGINT PRIMARY KEY,
    symbol         TEXT NOT NULL,
    magic          BIGINT,
    side           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    opened_at      TIMESTAMPTZ NOT NULL,
    closed_at      TIMESTAMPTZ,
    entry_price    DOUBLE PRECISION,
    exit_price     DOUBLE PRECISION,
    volume_in      DOUBLE PRECISION NOT NULL DEFAULT 0,
    volume_out     DOUBLE PRECISION NOT NULL DEFAULT 0,
    exit_count     INTEGER NOT NULL DEFAULT 0,
    gross_profit   DOUBLE PRECISION NOT NULL DEFAULT 0,
    commission     DOUBLE PRECISION NOT NULL DEFAULT 0,
    swap           DOUBLE PRECISION NOT NULL DEFAULT 0,
    fee            DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_profit     DOUBLE PRECISION NOT NULL DEFAULT 0,
    comment        TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT trades_side CHECK (side IN ('long', 'short')),
    CONSTRAINT trades_status CHECK (status IN ('open', 'closed'))
);

CREATE INDEX IF NOT EXISTS trades_symbol_opened_idx
    ON trades (symbol, opened_at DESC);
CREATE INDEX IF NOT EXISTS trades_symbol_status_idx
    ON trades (symbol, status);


-- ---------------------------------------------------------------------------
-- 5. Backtest runs -- every POST /backtest, inputs and outputs
-- ---------------------------------------------------------------------------
-- Failed runs are stored too (status='error'). A run that could not find bars
-- for its window is a fact about the window, and dropping it is how the same
-- unavailable range gets requested five times.
--
-- `engine` records WHICH backtest produced the row. POST /backtest runs the
-- legacy close-only engine and its numbers are systematically optimistic; a
-- stored result with no engine column would be indistinguishable from a
-- run_baseline report later on.
-- A run may cover SEVERAL symbols replayed onto one account, so `symbol` alone
-- stopped being able to describe one. It is kept as the human label ("XAUUSDm",
-- or "XAUUSDm + BTCUSDm") because every stored row already has one and the list
-- views read it; `symbols` is the queryable truth, and `sizing` holds the lots
-- per symbol, which a pair of scalar columns cannot -- 0.1 lots of gold and 0.1
-- of Bitcoin are the same number and a different bet.
--
-- The scalar `lot_size` / `scale_out_lots` stay, and stay populated for a
-- single-symbol run, so rows written before this change keep their meaning
-- rather than being migrated into a shape they were never recorded in.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id                BIGSERIAL PRIMARY KEY,
    engine            TEXT NOT NULL DEFAULT 'legacy',
    symbol            TEXT NOT NULL,
    symbols           TEXT[] NOT NULL DEFAULT '{}',
    start_date        TIMESTAMPTZ NOT NULL,
    end_date          TIMESTAMPTZ NOT NULL,
    initial_balance   DOUBLE PRECISION NOT NULL,
    lot_size          DOUBLE PRECISION,
    scale_out_lots    DOUBLE PRECISION,
    partial_fraction  DOUBLE PRECISION,
    sizing            JSONB NOT NULL DEFAULT '{}'::jsonb,
    status            TEXT NOT NULL DEFAULT 'ok',
    error             TEXT,
    result            JSONB,
    duration_ms       INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT backtest_runs_status CHECK (status IN ('ok', 'error'))
);

-- For databases created before the two columns existed. Both paths run this
-- file, and on a fresh volume these are no-ops.
ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS symbols TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS sizing JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Backfill, once: a pre-existing row named exactly one symbol, so its array is
-- that symbol. Guarded on emptiness, not on a version, so re-running cannot
-- overwrite a real multi-symbol row with a single-element array.
UPDATE backtest_runs SET symbols = ARRAY[symbol]
WHERE symbols = '{}' OR symbols IS NULL;

CREATE INDEX IF NOT EXISTS backtest_runs_created_idx
    ON backtest_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS backtest_runs_symbol_idx
    ON backtest_runs (symbol, created_at DESC);
-- Filtering the list by symbol has to find a combined run too, so it matches on
-- the array; without this index that becomes a sequential scan per poll.
CREATE INDEX IF NOT EXISTS backtest_runs_symbols_idx
    ON backtest_runs USING GIN (symbols);


-- ---------------------------------------------------------------------------
-- 6. UI preferences -- replaces localStorage
-- ---------------------------------------------------------------------------
-- Theme, active view and the backtest form's last values. Held as jsonb under a
-- `scope` key rather than a column per field: these are cosmetic client state
-- with no invariants worth a constraint, and a migration per new toggle would
-- be the wrong trade. Anything with a rule attached belongs in a table above.
--
-- Single-user by construction, like the rest of this API -- see the Safety
-- section of CLAUDE.md. 'default' is the only scope the dashboard writes.
CREATE TABLE IF NOT EXISTS ui_preferences (
    scope       TEXT PRIMARY KEY,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO ui_preferences (scope, data)
VALUES ('default', '{}'::jsonb)
ON CONFLICT (scope) DO NOTHING;


-- ---------------------------------------------------------------------------
-- 7. Account snapshots
-- ---------------------------------------------------------------------------
-- Append-only. Two jobs:
--
--   * `/stats` reads the newest row instead of re-querying MT5. The old
--     get_account_info() ran FOUR history_deals_get() calls -- daily, weekly,
--     monthly, yearly -- over IPC on every poll, and the dashboard polls every
--     5s. That is the same mistake update_performance_stats() was throttled to
--     once a minute to avoid, repeated in the account panel.
--   * the rows accumulate into a real equity curve, which nothing in the live
--     path previously recorded at all. `max_drawdown` in the bot stats was
--     initialised to 0.0 and never written.
--
-- `period_profits` stays ACCOUNT-WIDE (every deal, any magic), matching what
-- the panel has always shown. The per-bot figures come from `trades`, which is
-- this bot's positions only -- S1 keeps those two questions separate.
CREATE TABLE IF NOT EXISTS account_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    login           BIGINT,
    currency        TEXT,
    balance         DOUBLE PRECISION,
    equity          DOUBLE PRECISION,
    profit          DOUBLE PRECISION,
    margin          DOUBLE PRECISION,
    margin_free     DOUBLE PRECISION,
    leverage        BIGINT,
    drawdown_pct    DOUBLE PRECISION,
    period_profits  JSONB NOT NULL DEFAULT '{}'::jsonb,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS account_snapshots_captured_idx
    ON account_snapshots (captured_at DESC);


-- 1: initial schema.
-- 2: backtest_runs.symbols / .sizing -- a run can cover several symbols at once.
INSERT INTO schema_version (version) VALUES (1), (2)
ON CONFLICT (version) DO NOTHING;
