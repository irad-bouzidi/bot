# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
pip install -r requirements.txt          # runtime (MetaTrader5 Windows-only; psycopg2)
pip install -r requirements-dev.txt      # pytest, pytest-cov
npm install --prefix frontend

# Containers + database (frontend and Postgres; the API stays on the MT5 host)
docker compose up -d                     # docker-compose.yml is at the repo root
python -m backend.db.migrate             # apply schema + import data/settings.json once
python -m backend.db.migrate --check     # report connectivity/version, change nothing

# Tests -- must be run from the repo root (tests/conftest.py inserts it on sys.path).
# The whole suite passes with no MetaTrader 5 terminal, no `data/` directory and
# NO POSTGRES: tests/test_db_repository.py skips itself when no server answers.
python -m pytest
python -m pytest tests/test_backtest_engine.py::test_intrabar_stop_is_detected_even_when_close_recovers
python -m pytest -k lookahead
python -m pytest tests/test_db_repository.py   # needs the db container up (46 tests)
npm test --prefix frontend               # CRA/jest; App.test.tsx (25 tests)

# Run -- the API needs BOTH a live MT5 terminal and a reachable Postgres
python -m backend.main                   # FastAPI on 127.0.0.1:8000
npm start --prefix frontend              # dashboard on localhost:3000 (dev server)
npx concurrently "python -m backend.main" "npm start --prefix frontend"
# or serve the built dashboard from its container instead of `npm start`:
docker compose up -d --build frontend

# Research (offline, no MT5). ONE SYMBOL PER RUN -- averaging two instruments'
# edges lets a losing one hide behind a winning one.
python -m backend.scripts.run_baseline --symbol XAUUSDm --compare-legacy
python -m backend.scripts.run_baseline --symbol BTCUSDm --start 2025-09-01
# The centre-line exit is a flag now, defaulting from SYMBOL_CONFIG (currently
# OFF on both symbols) and printed at the top of every report. Pass it to
# reproduce a report stored before the flag existed -- they were all made with
# the rule ON:
python -m backend.scripts.run_baseline --symbol XAUUSDm --exit-at-mean
python -m backend.scripts.run_baseline --symbol XAUUSDm --no-exit-at-mean
# Costs the default run now charges, and the two flags that switch them off.
# --slippage-stop defaults to ONE TYPICAL SPREAD of the instrument, and swap
# comes from the spec sidecar; no report written before 2026-09-06 had either.
python -m backend.scripts.run_baseline --symbol BTCUSDm --slippage-stop 0 --no-swap
# The risk layer. ALL OFF by default, and off is a byte-for-byte no-op.
python -m backend.scripts.run_baseline --symbol XAUUSDm --risk-pct 0.5 --min-equity 100
# Grid + train/holdout split (one CSV row per cell, never a ledger), and
# where the money went in a stored ledger. See "Sweeps, walk-forward and
# ledger diagnosis" below -- these are how sections 11 and 16 of the brief
# are actually implemented.
python -m backend.scripts.sweep --symbols XAUUSDm --timeframes M5,H1 --param sl_pips=70,110,150
python -m backend.scripts.diagnose data/reports/XAUUSDm_20260906_152553_ledger.csv
# --sl/--tp are PRICE units, SYMBOL_CONFIG is pip COUNTS times a per-symbol pip.
# They now DEFAULT from backend/core/symbols.py -- gold 70x0.1 -> 7/10, Bitcoin
# 70x10.0 -> 700/1000 -- and the chosen numbers are printed at the top of the
# report, along with the pip the report's pips figures are counted in. Passing
# 7/10 for BTCUSDm would put a $7 stop on an $81,000 instrument.

# Data capture (MT5 host only)
python -m backend.data.snapshot --symbol XAUUSDm --start 2023-01-01
python -m backend.data.snapshot --symbol BTCUSDm --start 2025-09-01
python -m backend.data.snapshot --list                         # cache coverage
python -m backend.data.snapshot --symbol XAUUSDm --spec-only   # smoke-test the terminal
python -m backend.data.snapshot --symbol XAUUSDm --verify 2026-07
```

## Environment constraints

- **Python 3.8.10** is the target (pinned by the trading host). No `X | Y` unions; the
  codebase uses `typing.Optional/List/Dict` and `# type:` comment annotations. Keep that
  style.
- `pandas==2.0.3` / `numpy==1.24.4` are pinned to what works on that host. **pyarrow is
  not available on 3.8**, which is why the bar cache is `csv.gz`, not parquet.
- Windows + PowerShell is the dev environment. `MetaTrader5` will not import on non-Windows.
- `psycopg2-binary==2.9.9` for the same reason: it is the version with a verified
  cp38 Windows wheel. Building psycopg2 from source on the trading host needs a
  compiler and libpq that are not there. psycopg3 is not used.
- The frontend image is `node:24-alpine` for both stages, matching the npm major
  that produced `frontend/package-lock.json`. npm 10 (node:20) rejects that lock
  file. The runtime stage keeps node rather than switching to a web server
  because `serve` needs it and because the entrypoint uses node's
  `JSON.stringify` to write `env.js` — see `frontend/docker-entrypoint.sh`.

## Architecture

The project assumes **two machines**: MT5 is Windows-only and needs a logged-in terminal;
nothing else does. `data/` is the handoff — snapshot on the trading host, copy it over,
and all research runs offline and reproducibly. It is **committed**, not gitignored:
the bar cache, the contract sidecars, the stored reports and the sweep outputs are all
in the tree, which is what makes a quoted number in this file checkable against the run
that produced it.

**Three tiers now, not two.** The API and the bot threads run on the MT5 host;
the dashboard and Postgres run in containers (`docker-compose.yml` at the repo
root, both published to `127.0.0.1` only); the research stack runs anywhere and
touches **neither** MT5 nor Postgres. The last of those is load-bearing: `run_baseline`, the engine and
the indicators must stay runnable with nothing but `data/`, so nothing under
`backend/backtest/`, `backend/strategy/`, `backend/indicators/` or
`backend/data/` may import `backend.db`.

The backend is deliberately **not** containerised — it imports `MetaTrader5`.
The frontend container serves the built bundle as **static files only** (`serve`,
no nginx, no reverse proxy) and does **not** proxy the API; the browser calls
`127.0.0.1:8000` directly, so the API can stay bound to loopback. See the
README's "Why nothing proxies the API", which explains why that is a safety
decision rather than an omission.

The container layer is `docker-compose.yml` at the repo root plus three files
in `frontend/`: `Dockerfile`, `serve.json` (SPA fallback, cache rules, security
headers) and `docker-entrypoint.sh` (writes `env.js` from `BOT_API_BASE`, then
`exec`s the server). There is no `docker/` directory.

**MT5 import invariant.** Only two modules import `MetaTrader5`: `backend/bot_manager.py`
(live loop, reads + writes) and `backend/data/mt5_source.py` (reads). Everything else must
stay importable without a terminal — `tests/test_indicators_nw.py::test_importable_without_metatrader5`
guards part of this. Do not add an MT5 import anywhere else. (`requirements.txt` and the
README refer to `backend/execution/mt5_broker.py`; that file does not exist yet —
`backend/execution/`, `backend/live/` and `backend/risk/` are empty placeholder packages
for the intended extraction of order-sending out of `bot_manager.py`.) Note the research
risk layer is `backend/backtest/risk.py` and deliberately **not** the empty
`backend/risk/` package: `tests/test_db_invariants.py` parametrises the
"may not import `backend.db` or MetaTrader5" guard over `backtest`, so keeping it there
keeps it guarded.

### Two parallel implementations exist — know which one you are touching

This is the most important thing to understand before editing.

| | Live / API path | Research path |
|---|---|---|
| Strategy rules | inlined in `TradingBot.run` (`bot_manager.py`) | `backend/strategy/nw_envelope.py` |
| Scale-out / break-even | `TradingBot.manage_position` | engine rule 9 (`_resolve_bar`) |
| Backtest | `BotManager.run_backtest` → `simulate_legacy` — close-only, cost-free, scale-out modelled at the trigger level | `backend/backtest/engine.py` — next-bar-open fills, intrabar stops, cost model |
| Data | `mt5.copy_rates_*` direct | `MarketData` / `CachedMarketData` over `data/` |
| P&L | `price_diff * lot_size * profit_mult` | `SymbolSpec.pl()` from real tick value |
| Config | `SYMBOL_CONFIG` (`backend/core/symbols.py`), pip counts; sizing from Postgres | `NWConfig` + `BacktestConfig`, price units |
| Storage | Postgres (`backend/db/`) | `data/` files only — never Postgres |
| Sizing | `SYMBOL_CONFIG["lot_size"]` / `"partial_fraction"` / `"risk_pct"`, editable via `POST /settings` | `BacktestConfig.volume` / `NWConfig.partial_fraction` / `RiskConfig.risk_pct_per_trade`, CLI flags |
| Risk caps | **none** — no daily loss cap, no consecutive-loss cooldown, no equity floor, no margin check | `RiskConfig` (`backend/backtest/risk.py`), all off by default |
| Centre-line exit | `TradingBot._mean_reversion_exit`, gated on `SYMBOL_CONFIG["exit_at_mean"]` from Postgres | `NWEnvelopeStrategy.on_bar`, gated on `NWConfig.exit_at_mean` — **both default OFF** |

`POST /backtest` (used by the frontend Backtest page) still runs the **legacy** engine, so
its numbers are systematically optimistic and do not match `run_baseline`. It can now
cover several symbols at once -- see "Combined backtests" below; the optimism is per
symbol and does not cancel out when they are merged. The research
stack is the honest one; `BacktestConfig(legacy_mode=True)` reproduces the old behaviour
inside the new engine purely for regression comparison (`run_baseline --compare-legacy`
prints the overstatement). When changing strategy behaviour, expect to change it in
**both** places, or say clearly that you did not.

Two caveats on the legacy engine, both new:

- `simulate_legacy` now models the scale-out (banked at the trigger *level*, resolved
  before the exits), because the Backtest page lets you set the two lot numbers and a
  backtest that sized differently from live could not answer the question being asked.
  **Every `POST /backtest` number moved as a result** unless the scale-out is off.
  `tests/test_sizing_settings.py` pins `partial_fraction=0` against a verbatim copy of
  the pre-change loop, so the engine with the rule off is provably unchanged.
- `BacktestConfig(legacy_mode=True)` skips the whole intrabar block, scale-out included,
  so `--compare-legacy` and `POST /backtest` now agree only when `partial_fraction=0`.
  Do not read one as a check on the other.

### Combined backtests

`POST /backtest` takes `symbols` (a list) and per-symbol `sizing` in lots, and replays
several symbols onto **one account** in close-time order (`combine_legacy_results`).
That is the only reading of "both combined" a trader can act on -- two symbols funded
separately are just two backtests printed side by side.

The consequence is that the combined figures are **not** the per-symbol ones added up.
`max_drawdown` comes from the merged equity curve, because the interleaving is the whole
content of that number: two drawdowns that land together compound, two that offset do
not, and neither is recoverable from finished summaries. That is why `simulate_legacy`
returns `closed_trades` at all -- it is an input to the merge, stripped before the result
is returned or stored. Win/loss counts *do* add up, and `win_rate`'s denominator stays
`trades_opened` so it is comparable with the per-symbol figures printed beside it.

Sizing is per symbol because a lot is not a comparable unit across symbols. `run_baseline`
deliberately has no combined mode; run it twice.

The Backtest page's symbol chips come from `/settings`, so they always match
`SYMBOL_CONFIG` -- there is no second list to keep in step. **All assets** selects every
one of them. A failed `/settings` fetch is reported on the form instead of swallowed: the
fallback list is a single symbol, so swallowing it renders as "this bot only trades gold",
a plausible page with nothing on it to suggest anything is missing.

Storage: `backtest_runs` gains `symbols TEXT[]` and `sizing JSONB` (schema version 2).
`symbol` stays as the label (`"XAUUSDm + BTCUSDm"`), and `list_backtests(symbol=...)`
matches on the array too, so a combined run appears under either symbol's filter -- it is
a fact about both.

**Schema version 5** adds `symbol_settings.risk_pct` plus the two matching
`settings_audit` columns. Unlike version 3 it changes **no** behaviour: the column ships
as 0, which means "size from `lot_size`", so an existing row keeps trading exactly as it
did. `REQUIRED_SCHEMA_VERSION` moved to 5 anyway, for the same mechanical reason as
version 4 -- `load_settings()` NAMES the column, so a database left at 4 would fail inside
`_load_settings()` with a psycopg2 `UndefinedColumn`. It is also the first column with a
CHECK constraint applied to **existing** volumes: Postgres has no
`ADD CONSTRAINT IF NOT EXISTS`, so `schema.sql` uses the idempotent
`DROP CONSTRAINT IF EXISTS` + `ADD CONSTRAINT` pair, which
`tests/test_db_invariants.py` allows by name.

**Schema version 4** adds `trades.pips`. It changes no behaviour and no money figure,
but `list_trades()` and `trade_stats()` NAME the column, so a database left at version 3
fails `/trades` with a psycopg2 `UndefinedColumn` -- which is why
`REQUIRED_SCHEMA_VERSION` moved with it. The column arrives NULL and is filled by the
first `reconcile_all(full=True)`, i.e. the next API boot.

**Schema version 3** adds `symbol_settings.exit_at_mean` plus the two matching
`settings_audit` columns. `REQUIRED_SCHEMA_VERSION` in `bot_manager.py` is a **floor**,
not a "has any schema" check: `load_settings()` names the new column, so a database left
at version 2 would fail inside `_load_settings()` with a psycopg2 `UndefinedColumn` --
past the point where `init_persistence()` can still print the migrate command. Applying
version 3 also **changes live behaviour**, since the centre-line exit was unconditional
before it; `python -m backend.db.migrate` is what switches it off, and the container's
init-dir mount will not do it (Postgres ignores that directory once a volume exists).

### Persistence (`backend/db/`)

Everything the UI can change, and every trade, is in Postgres. Nothing that
matters is held in a process any more.

- `pool.py` — DSN from `BOT_DATABASE_URL` (default
  `postgresql://bot:bot@127.0.0.1:5432/tradingbot`, matching the compose file),
  a `ThreadedConnectionPool` because the bot threads and FastAPI both use it,
  and context managers that **roll back on any exception**. That rollback is not
  politeness: psycopg2 leaves a failed transaction open, the connection returns
  to a *pool*, and the next unrelated caller would inherit the poisoned one.
  **psycopg2 is imported lazily**, inside `_driver()`, so importing this module
  cannot fail on a host without the driver.
- `schema.sql` — the single source of truth, applied by `migrate.py` **and**
  mounted into the db container's init directory. Every statement is
  `IF NOT EXISTS`, because the container path only runs on a fresh volume so
  re-runnability is the primary path.
- `repository.py` — **all** the SQL. Two rules it exists to hold:
  *the store cannot widen its own reach* (`load_settings` SELECTs exactly the four
  `EDITABLE_KEYS` columns for exactly the symbols named, and validates every one), and
  *aggregates are derived, never accumulated* (win/loss/P&L are SELECTs over
  `trades`, which is folded from `deals`, so nothing can drift).
- `migrate.py` — `python -m backend.db.migrate`. Imports `data/settings.json`
  once, then leaves the database value alone on every later run.

**The API refuses to boot without Postgres** (`init_persistence`, called from the
FastAPI startup hook). This is deliberate. `lot_size` lives in the database and
is the only risk control this bot has, so booting on the 0.1 code default
because the database was unreachable would restore ~$70/trade for someone who
had deliberately lowered it — the exact silent restore the old
write-then-rename settings file existed to prevent. A `POST /settings` the
database refuses is likewise refused to the user rather than applied in memory.

**But a Postgres outage does not stop a bot that is already running.** Writes
from inside the live loop go through `_persist()`, which logs and continues. The
size it trades with is already in `SYMBOL_CONFIG`; halting would leave a real
position with nothing to fire its scale-out or move its stop to break-even,
which is strictly worse than a gap in the history. The gap is reported through
`GET /health` and the bot's `last_error`.

#### Tables

| Table | Replaces | Note |
|---|---|---|
| `symbol_settings` | `data/settings.json` | the four `EDITABLE_KEYS` only; CHECK constraints refuse a bad value on the way *in*, which a file could not. `exit_at_mean` is BOOLEAN and needs none -- the type is the constraint |
| `settings_audit` | nothing | append-only; the file overwrote its own history on every save |
| `bot_state` | instance attributes | `desired_state` + the S4 `last_bar_time`/`last_entry_bar` |
| `control_events` | nothing | every start/stop press, accepted or refused |
| `bot_snapshots` | `TradingBot.stats` | latest envelope reading, with `updated_at` |
| `deals` | nothing | raw MT5 deals, keyed on the broker's deal ticket |
| `trades` | nothing | one row per **position**, folded from `deals`; `pips` is derived in the same fold and NULLABLE |
| `backtest_runs` | nothing | every `POST /backtest`, inputs + outputs, errors included |
| `ui_preferences` | `localStorage` | theme, active view, backtest form; jsonb, merged not replaced |
| `account_snapshots` | nothing | throttled account reading; accumulates an equity curve |

#### The trade fold is a behaviour change, not just a schema one

`update_performance_stats()` counted every `DEAL_ENTRY_OUT` as its own win or
loss. `trades` groups by `position_id`, so **one trade is one outcome, decided
on net profit** (costs are *summed*, since MT5 signs commission/swap negative
already). Consequences, all measured on this repo's own live history:

- A trade that banks a scale-out and then stops at break-even was one win plus
  one silently-dropped zero. It is now one row with `exit_count = 2`. That
  arithmetic flattered precisely the rule the cached gold data measures as
  **negative** for expectancy.
- Filtering deals by `MAGIC_NUMBER` alone is **not enough**. A position closed by
  its own SL/TP can produce a deal whose magic is 0, and the old filter dropped
  it — so an SL-closed trade kept its entry and lost its exit. `_deal_rows()` is
  therefore two-stage: collect the position ids that have *any* deal bearing the
  magic, then keep every deal on those positions. On the live history here that
  recovered 2 of 21 trades and $3.36 of P&L the old code never counted.
- `max_drawdown` for a live bot was initialised to `0.0` and **never written**.
  It is now the deepest peak-to-trough of the closed-trade equity curve, in
  account currency — not the intrabar figure the research engine reports, and
  not a percentage (the live balance moves for deposits, so dividing by it would
  move the number without a trade happening).
- Break-even trades are excluded from the win-rate denominator and reported
  beside it as `breakeven`. With the stop moved to entry they are a designed
  outcome of the scale-out rule; counting them as losses understates it and as
  wins overstates it, so the count is returned and the choice is visible.

`reconcile_trades()` re-scans an **overlapping** window (a day before the newest
stored deal) rather than resuming exactly where it left off, because MT5 credits
`swap` to a deal after the fact. The upsert is keyed on the deal ticket, so
re-reading corrects a row instead of adding one; `full=True` re-scans the year
and runs once at boot and on each thread start.

#### Auto-resume is off by default

`bot_state.desired_state` records what the user last asked for, so a restart can
*show* that a bot was running — and `/stats` returns `status` (the live thread)
and `desired_state` separately, because collapsing them into one word is how a
dead bot came to report "Running". It does **not** restart it: starting live
trading with real money because a process came back up is not a decision an
unauthenticated API should make. The dashboard surfaces the mismatch and offers
the button. `BOT_AUTO_RESUME=1` opts in.

### Research stack seams

- `backend/core/types.py` — `SymbolSpec` (contract spec captured from the broker; its
  `pl()` is the single P&L implementation — do not reintroduce `profit_mult`), `Signal`,
  `Side`, `PositionView`.
- `backend/strategy/base.py` — `Strategy` receives a `BarContext` whose `features` are
  **scalars at one index**, so look-ahead is structurally unwritable; `test_no_lookahead`
  verifies it. Signals carry **distances, not prices**, so the simulated and live brokers
  each own their own fill price, rounding and stops-level clamping.
- `backend/backtest/engine.py` — the execution contract is in its module docstring and
  each rule has a test. Signals evaluate on bar *i*'s close and fill at *i+1*'s **open**;
  stops are live **from the fill**, entry bar included, and checked **intrabar** against
  high/low; a gap fills at the **gap price**, not the level; SL wins same-bar ties by
  default; drawdown comes from the **equity** curve. Changing any of these invalidates
  every stored report in `data/reports/` — which has happened once, see "The entry-bar
  blind spot" immediately below.

- `backend/data/cache.py` — `BarSet.warmup_count` + `eval_slice()`. Warm-up bars are
  prepended *before* the requested range and excluded from evaluation; the engine iterates
  from `warmup_count`. This exists because the old backtest silently lost the first ~998
  bars of every window to NaN bands.
- `backend/indicators/nadaraya_watson.py` — only the **non-repainting endpoint** branch of
  the Pine source is implemented. Two traps called out in its docstring: the kernel must
  **not** be reversed for `np.convolve`, and the denominator is always the **full-window**
  weight sum even when truncated. `taps` is a speed knob, not a tunable.
- `backend/backtest/risk.py` — `RiskConfig` + `size_for_risk()`. Sizing and the caps that
  bound a losing run, all off by default; see "The risk layer" below for the four
  decisions inside it that are not obvious.
- Missing data raises `DataUnavailable` naming the exact `snapshot` command to run, never
  silent NaNs.

### The entry-bar blind spot — the bias that made every stored number wrong

For as long as this engine existed the intrabar check was guarded by
`i > open_trade.entry_index`, so **the bar a trade filled on was never tested against its
own stop or target**. A signal fills at bar *i*'s open, and the first check then landed on
bar *i+1*'s open — which rule 5 dutifully booked as a *gap* fill, at whatever price the
skipped bar had run to.

The entry bar is the worst possible one to skip: entries fire on band penetration, i.e.
volatility expansion, and gold's median entry bar spans 8.13 against a 7.00 stop. Measured
on the last pre-fix gold report (2,303 trades):

| | |
|---|---|
| stop-outs exiting on the bar after entry | **717 of 1,044**, filling an average **3.015 past a 7.00 stop** |
| the same trades held ≥3 bars | ~0.10 past the stop, i.e. correct |
| take-profits doing it | **365 of 622**, filling **2.128 BETTER than the 10.00 target** — which a LIMIT order cannot do |
| share of all trades affected | **47%** |

The two errors do not cancel: the stop side was 2.8x the target side. In effect the engine
was charging ~30 pips of slippage on 70% of its stop-outs.

**Live was never affected.** `open_trade` puts `sl` and `tp` inside the entry
`TRADE_ACTION_DEAL` (`bot_manager.py`), so the broker has held both from the moment of fill
all along. The fix closes a research/live divergence; there is no live half to it.

Two things fell out of the fix that are worth knowing:

* **It does not make the strategy better.** Per-trade honesty is restored (`avg_loss` moved
  from $89.73 to $70.08, i.e. the actual stop; `largest_win` from $835 to $103), but the
  strategy now re-enters sooner and takes 22% more trades, so the total loss is unchanged
  or slightly worse. The artifact had been *throttling* re-entry. A ledger-level
  counterfactual that holds the trade set fixed says −$2,969; the real re-run says
  −$19,871. Do not quote the counterfactual.
* **`ambiguous_bars` had to be split.** On the entry bar the break-even stop sits at that
  bar's own open plus the spread, so "the low printed below it" is true of almost any bar
  and told you nothing — it would have taken the counter from 23% to 47% of trades and
  destroyed its meaning. `ambiguous_entry_bars` is now reported separately.

`_track_excursion` also moved to *before* the stop block, so the bar that closes a trade
still contributes its range. Without that, a trade opening and closing on one bar would be
written with a flat 0.0 MAE/MFE — and after this fix that is a third of them.

### Warm-up arithmetic

The envelope needs `window - 1` bars for the centre line plus `mae_window` for the rolling
MAE, so the first usable index is 998 at the defaults (`nw_warmup_bars()`) and 999 closed
bars are the minimum. Falling short yields all-NaN bands, which compare `False` against
every price — the failure mode is a bot that reports "Running" and never trades. Both
paths check for this explicitly; keep it that way. Note `mae_window=500` reproduces the
original code while Pine uses 499; the off-by-one is deliberate and configurable.

### Live loop invariants (`bot_manager.py`)

Each is a fix for a real incident. The numbering is a **documentation** convention, and
only `S1` and `S3`–`S7` are actually written as markers in `bot_manager.py`; `S8`, `S10`
and `S11` are described here and live inside the code they amend (the persisted `S4`
guards, `_mean_reversion_exit()`, and the scale-out guard respectively). `S2` never
existed. **`S9` is retired** — it was the news blackout, now removed — and the number is
deliberately left as a gap rather than reused, so an `S9` in an older comment or commit
still means what it said:

- `bot_positions()` filters by `MAGIC_NUMBER` — the bot must never touch manually opened
  positions.
- The still-forming bar (`iloc[-1]`) is dropped; act **once per closed bar**, with
  `COOLDOWN_BARS` between entries. Acting on the forming bar made live disagree with the
  backtest and could fire five entries per candle.
- Filling mode is derived from `symbol_info().filling_mode`, which is a **bitmask**
  (FOK=1, IOC=2) and does **not** share values with the `mt5.ORDER_FILLING_*` constants.
- `order_send` can return `None`; every rejection must log retcode, comment and
  `last_error()`.
- Bot threads are daemons and `stop_all()` runs on FastAPI shutdown.
- SL/TP are rounded to tick size and widened past the broker's minimum stop distance.
- `update_performance_stats()` scans 365 days of deal history over IPC — keep it out of
  the per-tick signal path (currently throttled to once a minute).
- `S7`: `manage_position()` (scale-out + break-even) runs **every ~15s cycle**, not once
  per bar, because the trigger is an intrabar event. It holds no per-ticket state of its
  own — it re-derives what is still to do from the position's SL and from what the
  position opened with, so it survives restarts. Entries and the mean-reversion exit
  remain gated per closed bar; do not move them.
- `S8`: the S4 bar marks are now **persisted** (`bot_state`). They were instance
  attributes, so Stop-then-Start — or any restart — cleared the cooldown and the
  bot could enter again on the very bar it had just entered on, which is the
  repeat-fire S4 exists to prevent, reachable from the dashboard's own buttons.
  Memory stays the working copy and is written through, so a Postgres outage
  degrades to the old behaviour rather than halting a bot holding a position.
- `S10`: the centre-line exit lives in `_mean_reversion_exit()`, extracted from `run()`
  rather than inlined. Two reasons. It reads `exit_at_mean` under `_CONFIG_LOCK` — unlike
  `pip` in `manage_position()`, this key is editable at runtime *and* editable while a
  position is open, so the loop can genuinely race a save. And nothing in the suite drives
  `run()`, so for as long as it was six inlined lines, the rule deciding most of this
  strategy's exits had no test at all.
- `S11`: "has the scale-out already fired?" is answered from the position's **own entry
  deal** (`repository.opened_volume`, keyed on the broker's `position_id`), not from
  `SYMBOL_CONFIG["lot_size"]`. The configured size is only a valid proxy while it is a
  constant between trades, and it stopped being one the moment `risk_pct` could derive it
  from equity — an already-reduced position would read as untouched and be scaled out a
  second time. This needed no new state: `deals` is already keyed on the position and
  already durable, so it is a SELECT rather than the per-ticket dict S7 refuses to carry
  across restarts. When the read fails the scale-out is **skipped and the stop still
  moves** — scaling out twice cannot be undone, skipping it costs part of one trade.

### Scale-out / break-even

At `be_trigger_pips` in profit (default: half the target), `partial_fraction` of the
position closes and the stop moves to entry. One rule, expressed as a distance and a
*fraction* in both paths — `SYMBOL_CONFIG` live, `NWConfig.be_trigger_mode` in the
strategy. Never express it as a lot count: 0.05 is 50% of the current 0.1 lot_size and
would silently become a different share of the position if the size changed. The
dashboard edits it in **lots** because that is what a trader types; `scale_out_fraction()`
is the single boundary that converts, and the resulting percentage is echoed back to the
form so a re-scale is visible rather than silent. Do not add a second conversion.

**The 9-setting sweep that used to be quoted here has been STRUCK** — it was produced on
the pre-fix engine, and the scale-out is the rule that bias touched hardest: the trigger is
reached on the ENTRY bar in a third of gold's trades, which the engine did not model at all.
The rule is still enabled because it was asked for, not because the data supports it, and it
still clips winners while leaving straight-to-stop losers untouched. Re-measure with
`sweep.py --param partial_fraction=0,0.25,0.5 --param be_trigger_pips=30,50,70` before
quoting a number.
`--no-breakeven`, `be_trigger_mode="none"` / `partial_fraction=0` in `SYMBOL_CONFIG`,
or a scale-out of **0 lots** in the dashboard's Position sizing panel, turns it off.
Re-run the comparison before drawing any conclusion from a report that predates it.

### Centre-line (mean-reversion) exit — a UI toggle, default OFF

The rule that closed a position when a closed bar printed back at the envelope's **centre**
line. It is now `SYMBOL_CONFIG["exit_at_mean"]`, the **third** `EDITABLE_KEYS` entry, and it
ships **off** on both symbols. `run_baseline` takes `--exit-at-mean` / `--no-exit-at-mean`
and defaults from `SYMBOL_CONFIG`; the dashboard has a switch in the bot card's *Exit rules*
block. With it off, a trade can only end at its stop, its break-even stop, or its target
(nothing else closes a position).

**Why it was turned off.** The centre line sits about `mult * mae` from entry — ~6.00 on
gold — which is *past* the 5.00 scale-out trigger and *short* of the 10.00 target. The rule
has **no scale-out awareness** (no volume check, no `be_moved` flag), so it raced the
break-even stop and the target on every trade that banked a partial. A live XAUUSDm short
entered at 4485.183 (SL 4492.183, TP 4475.183) banked half at 4480.183, moved its stop to
break-even, and was then closed here at **4479.196** — short of the target it had been left
running for. On the legacy engine that single trade is $54.94 with the rule on and $75.00
with it off.

**The previous census in this file was wrong**, and could not be checked: it claimed the
scaled-out remainder reached the target 55% / break-even 32% / centre line 13%, but
`data/reports/BTCUSDm_20260904_102130_ledger.csv` gives **signal 65.8% / be_stop 21.2% /
tp 13.0%** — the two ends were transposed, so the dominant exit read as the rare one. The
follow-on claim that disabling it "makes expectancy worse, so leave it on" had no report
behind it at all, because `exit_at_mean` was unreachable from the CLI until the flag existed.

**The measured A/B that used to sit here has been STRUCK.** It compared two runs that
were both produced by the engine's entry-bar blind spot (see "The entry-bar blind spot"
above), on a window where 47% of gold's trades exited through the artifact — so the
direction it reported is not evidence about the rule. It shipped OFF on both symbols
because one rule across both instruments was asked for, and that decision stands on its
own; the numbers that were offered in support of it do not. Re-measure with
`run_baseline --exit-at-mean` / `--no-exit-at-mean` on the fixed engine before quoting a
direction. The archived files are in `data/reports/pre-entry-bar-fix/`.

`cross_center` is now its own `exit_reason` in `backend/backtest/ledger.py` rather than
folding into `"signal"` — that fold is what made the census above uncheckable. **Reading an
older report: its `signal` rows are this `cross_center`.** Live, the equivalent is
`close_position(comment="NW mean reversion")`; it previously sent `close_position`'s default
comment, which is why the incident could not be attributed from `trades.comment`.

Note when reading TP averages: rule 5 fills a gapped level at the gap price, which is
correct for stop orders but optimistic for a take-profit LIMIT, where a broker fills at
the limit. It inflates the TP tail on both sides of any comparison, so it does not bias
an A/B — but do not read an average TP win as achievable.

## Safety

`POST /control` starts and stops **live trading with real money and has no
authentication**, and `POST /settings` changes the size of the orders it sends with just
as little. `BOT_HOST` defaults to `127.0.0.1` for that reason; do not change the default
or widen `BOT_ALLOWED_ORIGINS` unless asked. There is still no margin check, and
`lot_size` defaults to 0.1, so gold risks ~$70 a trade (a measured 9-11 loss streak is
~$630-770). The dashboard shows that dollar figure next to the field, and switches to
the equity percentage when `risk_pct` is on.

**Equity-based sizing now exists but ships OFF** (`risk_pct = 0.0`), and on a small
account it barely functions: gold risks $700 per 1.0 lot, so the broker's smallest
position already risks $7 — 0.23% of a $3,000 account. Anything below that floor sizes
under `volume_min` and the entry is **skipped**, never rounded up. A daily loss cap and
a consecutive-loss cooldown exist in the RESEARCH engine (`backend/backtest/risk.py`)
and are **not** wired into the live loop; do not read `--max-daily-loss-pct` as a live
control.

`lot_size`, `partial_fraction`, `exit_at_mean` and `risk_pct` are the **only** four keys
`POST /settings` can touch, and the only four persisted — now to `symbol_settings`
in Postgres, not `data/settings.json`. They are persisted because silently restoring
0.1 on restart would undo a size someone lowered on purpose, and restoring the
centre-line exit would undo a rule someone switched off on purpose. `risk_pct` is
persisted for the first reason again: it decides the size of a real order.
`_load_settings()` and `repository.load_settings()` are both narrow: only those keys,
only for symbols already in `SYMBOL_CONFIG`, only values that survive `_validated`.
A row must never be able to introduce a symbol or move a stop — and that matters
*more* with a database than it did with a file, because psql, a migration and
anything else holding the DSN can write rows the UI never could. `schema.sql` adds
CHECK constraints as a second line of defence, refusing a bad value on the way
**in**; the file store could only reject one on the way out, at the next load.

Three things to keep straight about the **third** key, `exit_at_mean`, since it is the
first non-float one:

- **It has no CHECK constraint, and that is not an omission.** `BOOLEAN NOT NULL`
  admits exactly two values, so for this column the *type* is the second line of
  defence.
- **Its failure direction is bounded**, which is what made a rule-changing flag safe
  to put in a writable table at all. A stray `true` can only close a position
  *earlier*, at a price the market is offering; a stray `false` can only leave the
  broker-side SL/TP and the break-even stop standing. Neither value can size an
  order, widen a stop, or open a position. `lot_size` has no such bound.
- **`_validated` now refuses a boolean under a numeric key.** `bool` is a subclass of
  `int`, so `float(True)` is `1.0` — a boolean landing under `lot_size` would have
  validated cleanly as 1.0 lots, ten times the shipped size, with every range check
  passing it. That hole did not exist until this function started seeing booleans.

And three about the **fourth**, `risk_pct`, which is the first key that can size a real
order without anyone touching `lot_size`:

- **It is a PERCENT, so 1.0 means 1%,** and `MAX_RISK_PCT = 5.0` is the ceiling. The
  ceiling is there to make the classic unit confusion unstorable: `0.5` meant as a
  fraction is harmless, but `50` meant as "half" is refused instead of risking half the
  account on one trade. The same bound is a CHECK constraint in `schema.sql`, so psql
  cannot write what the API refuses.
- **Its failure direction is NOT bounded**, unlike `exit_at_mean`. This is the key that
  reintroduces everything the `lot_size` refusal exists for, which is why it counts as a
  sizing edit and why it ships at 0.
- **A size below `volume_min` is SKIPPED, never clamped up** — `_risk_sized_lots()`
  returns `None` and the entry is dropped, matching `backend/backtest/risk.py`. Clamping
  would risk *more* than asked precisely when the account is smallest. On this account
  size that is not a corner case: see "Risk-% sizing barely functions on a small account".

A `POST /settings` carrying only `exit_at_mean` is **accepted while a position is
open**, unlike a sizing edit. That is the only key of the four that is. `risk_pct`
travels with `lot_size` and `scale_out_lots` in `touches_sizing` and is refused with
them; `exit_at_mean` removes an exit and sizes nothing, and the moment someone reaches
for it is while a trade is running and the centre line is closing in on it. Refusing it
then would withhold the control in the only situation that motivates it.

The database is published on `127.0.0.1:5432` only, for the same reason
`BOT_HOST` is loopback. `docker compose down -v` **deletes** the trade history
and the stored lot size along with the volume.

The compose project is pinned (`name: nw-bot`) and the volume is named
explicitly (`nw-bot-db-data`) rather than left to the `<project>_<key>` default.
Both exist because the default derives from the *directory name*, so moving or
renaming a directory swaps in an empty database — and an empty
`symbol_settings` is exactly the silent restore of the 0.1 `lot_size` default
that `init_persistence` refuses to boot for. When this file lived in `docker/`
the volume was `docker_db-data`; carry it over rather than starting fresh:

```powershell
docker run --rm -v docker_db-data:/from:ro -v nw-bot-db-data:/to alpine sh -c 'cd /from && tar cf - . | (cd /to && tar xf -)'
```

A **sizing** edit is refused while the bot holds a position, under the same
`_CONFIG_LOCK` that `open_trade()` holds across its `order_send`.

**The original reason for that refusal is gone, and the refusal is not.** It existed
because `manage_position()` decided "has the scale-out already fired?" by comparing the
position's volume against `lot_size`, so lowering the size mid-trade made an
already-reduced position look untouched and scaled it out twice. `S11` removed that
inference — the guard now reads the position's own entry deal — and the objection that
remembering the size per ticket would need the cross-restart state S7 avoids turned out
to be answerable without any new state at all, because `deals` was already keyed on the
position.

What the refusal now protects is narrower and still worth having: the size a *running*
trade was opened at is the size its scale-out and its risk were reasoned about, and
changing the configured size — or switching `risk_pct` on — mid-trade produces a
dashboard that describes a position that does not exist. It is a legibility guarantee
now rather than a correctness one. If it is ever relaxed, `S11` is the invariant that
has to keep holding, not this lock.

### Symbols

`XAUUSDm` and `BTCUSDm` are configured, both in **`backend/core/symbols.py`** --
`SYMBOL_CONFIG` moved there out of `bot_manager` so that `backend.db.migrate` (no
terminal) and `run_baseline` (no terminal, no database) can read the same table
instead of copying it. `bot_manager` re-exports the same dict object, so a running bot
still holds a live reference into it and a size edit still reaches the thread.

| | XAUUSDm | BTCUSDm |
|---|---|---|
| `pip` | 0.1 ($1 = 10 pips) | 10.0 ($100 = 10 pips) |
| stop / target | 70 / 100 pips = 7.00 / 10.00 | 70 / 100 pips = 700 / 1000 |
| scale-out trigger | 50 pips = 5.00 | 50 pips = 500 |
| `profit_mult` (contract size) | 100 oz per lot | 1 BTC per lot |
| `pip_value_per_lot` (derived) | $10 | $10 |
| risk at the 0.1 default | ~$70 | ~$70 |
| `exit_at_mean` | `False` (editable) | `False` (editable) |
| `risk_pct` | `0.0` = off (editable) | `0.0` = off (editable) |

The pip COUNTS are identical on purpose -- one rule, two instruments -- so the worked
example reads the same on both: a BTCUSDm long at 80500 targets 81500, stops at 79800,
and at 81000 banks `partial_fraction` of the position and pulls the stop to 80500.

**Bitcoin's `pip` was 1.0 with counts of 700/1000/500** until pips became a reported
figure. The product is identical -- 70 x 10.0 is the same 700.00 of price 700 x 1.0 was
-- so **no stop, target, trigger or P&L moved**, and the worked example above is
unchanged. What moved is what a pip is *called*: the old definition reported ten Bitcoin
pips for every one this project now reports. It also made the "identical pip counts"
claim above finally true of the table rather than only of the geometry.

**The equal $70 is a coincidence of the two contract sizes, not a rule.** Gold is 100 oz
over a 7.00 stop, Bitcoin 1 BTC over a 700.00 one. A third symbol will land wherever its
contract size puts it, so re-derive the dollar risk rather than assuming 0.1 lots means
$70. `TradingBot.__init__` and `run_backtest` now **refuse** an unconfigured symbol
instead of falling back to gold's row, because that fallback is silent and gold's pip
would compute a $0.70 stop on an $81,000 instrument.

### Pips -- a second measurement, not a restatement of the P&L

Every result surface now reports **pips won and pips lost** beside the money: the trade
history (`trades.pips`, schema version 4), `POST /backtest`, the dashboard's bot card and
`run_baseline`. One definition, `backend/core/symbols.py`:

```python
to_pips(symbol, price_diff)     # signed distance / SYMBOL_CONFIG[symbol]["pip"]
pip_size(symbol)                # 0.0 for a symbol not in the table
```

Four things to keep straight before reading or extending any of it:

- **Pips are GROSS and blind to size.** A price distance cannot carry a commission or a
  swap, and it does not move when the lot size does. That is the point: it is the number
  that stays comparable between two runs sized differently, and the gap between it and
  the money is what the costs took. It is *not* a share of the P&L. Read "blind to size"
  strictly: **every pips figure is measured as though the position were 0.01 lots**,
  which is what makes the scale-out rule below invisible to it.
- **The pip buckets are signed by the PIPS, not by the money.** `pips_won >= 0 >=
  pips_lost` always, and `net_pips` is the two **summed** (the same convention `avg_loss`
  uses). So the split can disagree with `wins`/`losses`: a trade that gained a pip and
  paid more than that in costs is a pip win and a money loss. Both counts are returned --
  `pip_wins`/`pip_losses` beside `wins`/`losses` -- so the disagreement is visible rather
  than looking like one of the two is wrong. The legacy engine is cost-free, so there the
  two can only agree; the research engine and the live fold are where they part.
- **NULL is not zero.** `trades.pips` is nullable and every UI renders it as an em dash.
  A closed trade with no pip figure means the symbol left `SYMBOL_CONFIG`, or the row was
  folded before version 4 and has not been re-read; `trade_stats` returns
  `pips_unknown` so the sums can be read as covering every closed trade or explicitly
  not. There is deliberately **no backfill UPDATE** in `schema.sql`: the pip size lives
  in code, and a copy of it in the schema would be a second table a new symbol could be
  missing from. `reconcile_all(full=True)` on every API boot re-folds the whole history,
  which is the migration.
- **A scaled-out trade is measured entry to FINAL exit**, and the banked leg contributes
  nothing: half out at +50 with the runner to the +100 target is **100 pips**, and half
  out at +50 with the runner scratched at break-even is **0 pips** on a trade that made
  money. That follows from the rule above -- 0.01 lots cannot be scaled out at any
  broker, so at the size pips are measured in, the partial does not exist. A trade still
  running on a banked partial has **no** pip figure (NULL), for the same reason.

  **This replaced a volume-weighted definition** (75 and 25 for the two cases above),
  which read as the more honest answer and was not: weighting by volume put the lot size
  back inside the one number that exists to be free of it. The identical price path
  reported **100 pips at 0.01 lots** (too small for the broker to split, so the rule
  never fired), **66.7 at 0.03** (MT5's volume step lands the split on 0.02 against 0.01)
  and **75 at 0.10**. Two runs of one strategy at two sizes could not be compared, which
  is the whole job of the column. Changed in all four places at once -- the live fold's
  SQL, `simulate_legacy`, the research engine and the stored ledgers -- so a pip still
  means one thing on every surface. `exit_price` is **unchanged** and stays the
  volume-weighted average the position actually left at; the fold derives pips from a
  separate `final_exit_price` instead, because the average is still the right answer for
  the money and for what the trade history displays.

  **Every stored pips figure predates this and is the old definition.** `trades.pips` is
  re-derived by `reconcile_all(full=True)` on the next API boot; `data/reports/*` and the
  `backtest_runs` rows are not re-derived by anything.

  **No money figure moved**, and that was measured rather than assumed:
  `run_baseline --symbol XAUUSDm --exit-at-mean` was run on the engine either side of the
  change and the two `metrics.json` compared key by key -- 201 keys, 0 non-pips
  differences, across all three cost scenarios. What the pips keys did on that run is
  worth knowing before reading one: `avg_win_pips` 64.4 -> 94.8 (a scale-out winner is no
  longer diluted by its banked leg), `net_pips` -16,819 -> -15,096, and `pip_wins` 1,232
  -> 871 with 159 trades now sitting at exactly 0 pips -- the break-even scratches. The
  old figure's real problem shows up in the same run: at 0.1 lots on gold it equalled
  `gross_pl` to the cent (ratio 1.0000), because volume-weighting had quietly made the
  column a restatement of the money that the first bullet says it must not be.

Summing pips across symbols (the combined backtest) adds **movement, not money**. It
happens to be readable in money here because `pip * profit_mult` is $10 a lot on both
configured symbols -- a coincidence of the two contract sizes, exactly like the equal
$70 risk -- so the combined view also prints `pips_by_symbol`, and the note under it is
derived from `/settings`'s `pip_value_per_lot` rather than written into the prose.

The research engine takes its pip through `BacktestConfig.pip_size` and does **not**
import `SYMBOL_CONFIG`; `run_baseline` resolves it, exactly as it already does for
`--sl`/`--tp`, so `backend/backtest/` stays runnable with nothing but `data/`. A run with
no pip defined reports `None`, and the report prints a dash rather than a `0` that would
read as a strategy that captured nothing. **Stored ledger CSVs gain two columns**
(`pips`, `pip_size`); no existing column's values moved.

Adding a third symbol means a `SYMBOL_CONFIG` entry and nothing else in the frontend --
the Backtest page reads its symbol list from `/settings`, which is keyed off
`SYMBOL_CONFIG`. Still do not add one without a backtest showing its R:R works.

**Both symbols measure NEGATIVE, and the numbers below are the first ones produced by an
engine that resolves the entry bar.** Shipped config (70/100/50 pips, scale-out on,
centre-line exit off), central costs, 0.1 lots on $1,000, full cached M5 span:

| | XAUUSDm | BTCUSDm |
|---|---|---|
| trades | 2,807 | 1,279 |
| win rate | 52.0% | 57.2% |
| net P&L | −$24,181 | −$3,429 |
| profit factor | 0.76 | 0.91 |
| expectancy | −0.12 R | −0.04 R |
| max drawdown | 2313% | 326% |
| stored report | `XAUUSDm_20260906_152553_*` | `BTCUSDm_20260906_153640_*` |

Bitcoin's swap is finally charged and it is not negligible on the long side:
`swap_long = -1638.6` points a day, triple on Friday, against `swap_short = 0`. Longs lose
$2,261 of the $3,429 against shorts' $1,168, and no report before 2026-09-06 charged it at
all — so a long-vs-short comparison on an older report is partly a financing artefact.

Bitcoin is the *less bad* of the two at the same nominal risk, and it is still a losing
configuration. The drawdown percentages remain uninterpretable for the reason the README
gives -- a fixed 0.1 lots on $1,000 runs the balance negative and there is no equity floor
-- so read the expectancy, not the drawdown.

**The headline is not sensitive to the stop-slippage assumption**, which is the one number
in the cost model somebody had to choose (`--slippage-stop`, defaulting to one typical
spread of the instrument). Across 0 / 0.5 / 1 / 2 spreads gold spans −$19,871 to −$24,181
and Bitcoin −$2,700 to −$3,429. Both stay negative at every point.

### What the sweep found: no configuration survives out of sample

`backend/scripts/sweep.py` was run over `sl_pips` x `tp_pips` x timeframe on both symbols.
Two results, and the second is the one that matters.

**160 cells on a common 11-month window** (M5/M15/M30/H1, sl 70-150, tp 100-220): **not one
cell has positive expectancy.** The best anywhere is BTCUSDm M30 150/140 at −0.038 R. Gold's
best is M15 150/220 at −0.097 R. The shipped gold configuration (M5 70/100) lands at
−0.142 R, among the worst gold cells in the grid.

Two directions do show up consistently, and both follow from the ATR mismatch below:

* **A wider stop is better on gold** at every timeframe -- 70 -> 150 pips improves
  expectancy monotonically, and it held on the held-out window as well as in training.
* **A fixed 70-pip stop degrades badly as the timeframe rises** on gold: M5 −0.142 R,
  M15 −0.208, M30 −0.359, H1 −0.467 (a 28% win rate). The stop is a shrinking fraction of
  one bar's range, so it is hit by noise.

**Gold H4 over twelve years (2014-2026), and this is the finding to remember.** Trained on
the first 70% the best cell is sl 260 / tp 160 at **−0.025 R**, profit factor 0.859 --
close enough to break-even to be tempting. On the held-out final 30% the *same* cell is
**−0.188 R**, profit factor 0.478, and its drawdown goes from 138% to 548%.

That is a seven-fold degradation out of sample, and it is the first time this project has
ever measured one, because until `sweep.py` existed there was no held-out window to measure
against. Read it as the answer to "which parameters should we ship": on this data, a
configuration selected in-sample carries no information about the next period. Anything
chosen from the in-sample column of a sweep is a fit to 2014-2023 gold, not an edge.

### Why there is no edge: the premise is false on gold and too weak on Bitcoin

The sweep says no configuration works. This says why, and it is not a tuning
problem — three independent measurements agree, and none of them involves a stop,
a target or a cost model.

**1. Costs are not the explanation on gold; they are the whole explanation on Bitcoin.**
Re-run with the cost model switched off entirely (`--no-costs --slippage-stop 0 --no-swap`):

| gross of all costs | XAUUSDm | BTCUSDm |
|---|---:|---:|
| net P&L | −$13,133 | **+$221** |
| profit factor | 0.86 | **1.01** |
| expectancy | −0.07 R | **0.00 R** |

Bitcoin's signal is a coin flip that costs turn into a loss. Gold's signal loses
money before a single cost is charged.

**2. Gold does not mean-revert at these horizons.** Variance ratio (VR < 1 is
mean-reverting, 1.0 is a random walk, > 1 trending), on log returns:

| | q=2 | q=5 | q=10 | q=20 | q=50 |
|---|---:|---:|---:|---:|---:|
| XAUUSDm M5 | 1.023 | 1.008 | 1.051 | 0.998 | 0.974 |
| XAUUSDm H1 | 1.000 | 0.994 | 1.012 | 1.058 | 1.015 |
| BTCUSDm M5 | 1.010 | 0.967 | 0.954 | 0.936 | 0.892 |
| BTCUSDm H1 | 0.969 | 0.962 | 0.902 | 0.932 | 0.924 |

Gold is a random walk to three decimal places at every horizon this strategy
trades. **There is no reversion on gold to harvest**, so no entry filter,
geometry or timeframe can find one — they can only move the costs around, which
is exactly what the 160-cell sweep showed. Bitcoin genuinely does revert (VR
0.89–0.94), which is why its gross expectancy is 0.00 rather than negative, and
the effect is smaller than one round trip's spread.

**3. The band touch adds nothing beyond the drift, and what it does add flips
sign between periods.** Event study, execution rules stripped away entirely:
entry at the next bar's open (rule 2), exit h bars later, measured in ATR14 so
the instruments are comparable, against the unconditional forward move over the
same horizon as the baseline. Drift-adjusted 20-bar edge, per 2-year block:

| | blocks | positive | mean edge | sign stable? |
|---|---:|---:|---:|---|
| XAUUSDm H1, fade the lower band | 10 | 3 | −0.372 ATR | **no** |
| XAUUSDm H1, fade the upper band | 10 | 5 | −0.166 ATR | **no** |
| BTCUSDm H1, fade the lower band | 9 | 2 | −0.427 ATR | **no** |
| BTCUSDm H1, fade the upper band | 9 | 2 | −0.467 ATR | **no** |

The one place a real-looking effect shows up is gold M5 over 2025-26, where the
LOWER band carries +0.114 ATR at 20 bars with t = 3.8 while the UPPER band is
strongly anti-predictive (−0.415 ATR, t = −7.0) — i.e. price kept rising after
touching the upper band, through a window in which gold rose 43%. That is not a
reversion edge, it is the bull run, and the block table above is what happens
when you look for it anywhere else: gold H1's own 2025 and 2026 blocks put the
long-side edge at −0.376 and −0.872.

**Do not act on the "long-only gold" reading.** It is the same seven-fold
out-of-sample collapse the H4 sweep found, observed at the signal level instead
of the P&L level, and it is the exact shape `Trading Bot.md` section 11 exists to
stop.

One consequence worth keeping if the strategy is ever revisited: gold M5's
long-side effect at h=20-50 bars, real or not, is at a horizon the geometry
cannot reach. `avg_mae_r` is −0.92 and the average hold is ~2 bars, so a 1.4-ATR
stop resolves the trade long before a 20-bar move could pay for it. Any future
attempt to collect a slow signal has to widen the stop *and* lengthen the hold
together; doing one without the other just changes which end the loss comes from.

### The risk layer (`backend/backtest/risk.py`)

`RiskConfig` adds what `Trading Bot.md` section 7 asked for and nothing had:
`risk_pct_per_trade`, `max_daily_loss_pct`, `max_consecutive_losses` +
`cooldown_bars`, `min_equity`, `max_open_positions`. Exposed on `run_baseline`
as `--risk-pct`, `--max-daily-loss-pct`, `--max-consecutive-losses`,
`--cooldown-bars`, `--min-equity`.

**It is off by default and off is a byte-for-byte no-op** — `RiskConfig()`
reproduces the fixed-volume engine exactly, pinned by
`test_risk_config_defaults_reproduce_the_fixed_volume_engine_exactly` and
confirmed by an identical ledger md5 against the pre-risk-layer run. A non-zero
default would have silently re-sized every stored configuration the moment the
file landed.

Four decisions inside it that are not obvious:

- **A size below the broker's minimum is SKIPPED, not clamped up.**
  `SymbolSpec.round_volume` clamps *up* to `volume_min`, so clamping would risk
  more than asked precisely when the account is smallest. `min_volume_policy`
  can opt into clamping; `entries_skipped_too_small` counts what was dropped.
- **The daily loss cap blocks entries and never closes a running position.**
  Closing one realises the loss the cap exists to bound.
- **The equity floor is terminal and produces `EXIT_RISK`.** It queues an
  ordinary EXIT signal so the fill still obeys rule 2, which is why `risk_halt`
  had to join `SIGNAL_EXIT_REASONS` — otherwise the one exit meaning "this run
  stopped early" would read as an ordinary strategy exit. `metrics["halted"]` is
  what a sweep must filter on: a halted run's `total_pl` is not comparable with
  a completed one.
- **`max_open_positions != 1` raises.** `open_trade` is a scalar, so the engine
  is single-position by construction, and a config it cannot honour must be
  refused rather than silently downgraded into a different backtest.

#### What it measures: sizing cannot rescue a negative edge

Gold, shipped config, central costs, equity floor at 10% of the balance:

| $100,000 account | trades | net P&L | expectancy | max DD | halted |
|---|---:|---:|---:|---:|---|
| fixed 0.1 lots | 2,807 | −$24,181 | −0.12 R | **24.9%** | no |
| 0.25% of equity | 2,807 | −$57,361 | −0.12 R | 58.3% | no |
| 0.5% of equity | 2,807 | −$82,203 | −0.12 R | 83.0% | no |
| 1.0% of equity | 1,853 | −$90,029 | −0.12 R | 90.1% | **yes** |
| 2.0% of equity | 992 | −$90,072 | −0.11 R | 90.3% | **yes** |

`expectancy_r` is flat at −0.12 across the whole column, which is the point:
**sizing scales the outcome and does not touch the edge.** With a negative
expectancy the drawdown-minimising risk fraction is zero, and there is no
setting that turns this into a fundable configuration. What the layer buys is
that drawdown is now a readable number at all — 24.9% instead of the 2313% a
fixed 0.1 lots on $1,000 used to report, which was the arithmetic of a balance
that had gone below zero.

#### Risk-% sizing barely functions on a small account, and that is a real limit

On a **$1,000** account the same sweep skips almost every trade:

| $1,000 account | trades taken | trades skipped, size below 0.01 lots |
|---|---:|---:|
| 0.25% of equity | **0** | 4,456 |
| 0.5% of equity | **0** | 4,456 |
| 1.0% of equity | 221 | 4,043 |
| 2.0% of equity | 455 | 3,551 |

Gold risks **$700 per 1.0 lot** at the 70-pip stop, so the broker's smallest
position — 0.01 lots — already risks **$7**. On $1,000 that is 0.7%; on the
$3,000 demo account it is **0.23%**. Any `risk_pct` below that floor is
unreachable and every entry is skipped. Fixed 0.1 lots on $1,000, meanwhile,
**halts**: it hits the floor after 96 trades having lost 92.7%.

So on an account this size the lot size really is the only risk control there
is, exactly as the Safety section says — not because risk sizing is missing any
more, but because the contract size quantises it away.

### The stop is not a fixed fraction of what either instrument does

Measured on the M5 cache (ATR14, and the NW band half-width the signal is built from):

| | XAUUSDm | BTCUSDm |
|---|---:|---:|
| ATR14 | 5.04 (50 pips) | 142.3 (14 pips) |
| NW band half-width | 13.84 | 388.8 |
| the 70-pip stop as x ATR14 | **1.39** | **4.92** |
| the 70-pip stop in band half-widths | 0.51 | 1.80 |

Identical pip COUNTS therefore produce two different strategies: gold risks 1.4 bars of
range, Bitcoin nearly 5. That is the whole of gold's `avg_mae_r` −0.92 against Bitcoin's
−0.47, and of gold's 53% win rate against Bitcoin's 62%. The counts are kept identical by
decision, so the free knob that can still bring the two into line is the **timeframe** --
70 pips is 1.39x ATR on gold M5 and 1.33x on Bitcoin H1.

The same quantity per trade (`r_price / band_at_entry`, both already ledger columns) sorts
the results monotonically on both symbols in opposite market trends, which makes it the
most robust single signal in this data and the first filter worth testing.

## Working conventions

`Trading Bot.md` is the standing brief for this repo, and its rules govern strategy work:
establish a baseline before modifying anything, back-test every meaningful change, never
use future information, never hide losing trades, never promise profitability, and do not
implement an "improvement" the data does not support. Prefer drawdown reduction over
headline P&L.

Comments here explain *why a defect was possible*, not what the line does. When fixing a
subtle bug, follow that pattern rather than stripping it.

### Sweeps, walk-forward and ledger diagnosis

Two scripts that did not exist, and without which `Trading Bot.md` sections 11 and 16 are
unimplementable. Both are offline, both reuse `BacktestEngine` + `compute_metrics`
in-process, and neither ever shells out to `run_baseline` (that would write a ~1 MB ledger
per cell to recover numbers already in memory).

```bash
# grid + train/holdout split; one tidy CSV row per cell, never a ledger
python -m backend.scripts.sweep --symbols XAUUSDm,BTCUSDm --timeframes M5,M15,M30,H1     --param sl_pips=70,90,110,130,150 --param tp_pips=100,140,180,220
# where the money went in any stored ledger
python -m backend.scripts.diagnose data/reports/XAUUSDm_<stamp>_ledger.csv
```

Three properties of `sweep.py` that are structural rather than conventional:

* **The geometry grid is in pip COUNTS, shared across symbols.** `price_levels()` turns them
  into per-symbol price distances, so a cell that gave two instruments different counts is
  not expressible. That is "one rule, two instruments" enforced by the type of the grid
  instead of by somebody remembering.
* **The objective is a t-statistic** (`expectancy_r / expectancy_r_se`), so a spectacular
  result on twelve trades cannot win a sweep.
* **Ranking is on the NEIGHBOURHOOD, not the cell.** Each row carries `nbhd_median`,
  `nbhd_std` and `spike_ratio`; a cell that beats its own neighbours is the `RSI = 53` case
  section 11 warns about and is visible as such. Edge cells report `n_neighbours` honestly
  so `--min-neighbours` can drop them.

`diagnose.py` leads with the **bars-held histogram**, deliberately: a spike at 0 or 1 bars
is the fingerprint of an execution rule resolving on the wrong bar, and it prints a warning
when one bucket dominates. That is the diagnostic which would have caught the entry-bar
blind spot years earlier, so it now runs first.

`AGENTS.md` holds a short subset of the commands above.
