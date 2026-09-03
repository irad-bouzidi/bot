# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
pip install -r requirements.txt          # runtime (MetaTrader5 Windows-only; psycopg2)
pip install -r requirements-dev.txt      # pytest, pytest-cov
npm install --prefix frontend

# Containers + database (frontend and Postgres; the API stays on the MT5 host)
docker compose -f docker/docker-compose.yml up -d
python -m backend.db.migrate             # apply schema + import data/settings.json once
python -m backend.db.migrate --check     # report connectivity/version, change nothing

# Tests -- must be run from the repo root (tests/conftest.py inserts it on sys.path).
# The whole suite passes with no MetaTrader 5 terminal, no `data/` directory and
# NO POSTGRES: tests/test_db_repository.py skips itself when no server answers.
python -m pytest
python -m pytest tests/test_backtest_engine.py::test_intrabar_stop_is_detected_even_when_close_recovers
python -m pytest -k lookahead
python -m pytest tests/test_db_repository.py   # needs the db container up (32 tests)
npm test --prefix frontend               # CRA/jest; App.test.tsx (4 tests)

# Run -- the API needs BOTH a live MT5 terminal and a reachable Postgres
python -m backend.main                   # FastAPI on 127.0.0.1:8000
npm start --prefix frontend              # dashboard on localhost:3000 (dev server)
npx concurrently "python -m backend.main" "npm start --prefix frontend"
# or serve the built dashboard from its container instead of `npm start`:
docker compose -f docker/docker-compose.yml up -d frontend

# Research (offline, no MT5)
python -m backend.scripts.run_baseline --symbol XAUUSDm --compare-legacy
# NB: --sl/--tp are PRICE units, while SYMBOL_CONFIG is in "pips" (pip = 0.1).
# XAUUSDm's live 70/100 pips is the 7/10 default here.
python -m backend.scripts.run_baseline --symbol XAUUSDm --start 2025-09-01

# Data capture (MT5 host only)
python -m backend.data.snapshot --symbol XAUUSDm --start 2023-01-01
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
- The container build stage is `node:24-alpine`, matching the npm major that
  produced `frontend/package-lock.json`. npm 10 (node:20) rejects that lock file.

## Architecture

The project assumes **two machines**: MT5 is Windows-only and needs a logged-in terminal;
nothing else does. `data/` (gitignored, but populated here) is the handoff — snapshot on
the trading host, copy it over, and all research runs offline and reproducibly.

**Three tiers now, not two.** The API and the bot threads run on the MT5 host;
the dashboard and Postgres run in containers (`docker/`, both published to
`127.0.0.1` only); the research stack runs anywhere and touches **neither** MT5
nor Postgres. The last of those is load-bearing: `run_baseline`, the engine and
the indicators must stay runnable with nothing but `data/`, so nothing under
`backend/backtest/`, `backend/strategy/`, `backend/indicators/` or
`backend/data/` may import `backend.db`.

The backend is deliberately **not** containerised — it imports `MetaTrader5`.
nginx serves the dashboard but does **not** proxy the API; the browser calls
`127.0.0.1:8000` directly, so the API can stay bound to loopback. See
`docker/README.md`, which explains why that is a safety decision rather than an
omission.

**MT5 import invariant.** Only two modules import `MetaTrader5`: `backend/bot_manager.py`
(live loop, reads + writes) and `backend/data/mt5_source.py` (reads). Everything else must
stay importable without a terminal — `tests/test_indicators_nw.py::test_importable_without_metatrader5`
guards part of this. Do not add an MT5 import anywhere else. (`requirements.txt` and the
README refer to `backend/execution/mt5_broker.py`; that file does not exist yet —
`backend/execution/`, `backend/live/` and `backend/risk/` are empty placeholder packages
for the intended extraction of order-sending out of `bot_manager.py`.)

### Two parallel implementations exist — know which one you are touching

This is the most important thing to understand before editing.

| | Live / API path | Research path |
|---|---|---|
| Strategy rules | inlined in `TradingBot.run` (`bot_manager.py`) | `backend/strategy/nw_envelope.py` |
| Scale-out / break-even | `TradingBot.manage_position` | engine rule 9 (`_resolve_bar`) |
| Backtest | `BotManager.run_backtest` → `simulate_legacy` — close-only, cost-free, scale-out modelled at the trigger level | `backend/backtest/engine.py` — next-bar-open fills, intrabar stops, cost model |
| Data | `mt5.copy_rates_*` direct | `MarketData` / `CachedMarketData` over `data/` |
| P&L | `price_diff * lot_size * profit_mult` | `SymbolSpec.pl()` from real tick value |
| Config | `SYMBOL_CONFIG` dict, pips; sizing from Postgres | `NWConfig` + `BacktestConfig`, price units |
| Storage | Postgres (`backend/db/`) | `data/` files only — never Postgres |
| Sizing | `SYMBOL_CONFIG["lot_size"]` / `"partial_fraction"`, editable via `POST /settings` | `BacktestConfig.volume` / `NWConfig.partial_fraction`, CLI flags |

`POST /backtest` (used by the frontend Backtest page) still runs the **legacy** engine, so
its numbers are systematically optimistic and do not match `run_baseline`. The research
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
  *the store cannot widen its own reach* (`load_settings` SELECTs exactly the two
  `EDITABLE_KEYS` columns for exactly the symbols named, and validates both), and
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
| `symbol_settings` | `data/settings.json` | the two `EDITABLE_KEYS` only; CHECK constraints refuse a bad value on the way *in*, which a file could not |
| `settings_audit` | nothing | append-only; the file overwrote its own history on every save |
| `bot_state` | instance attributes | `desired_state` + the S4 `last_bar_time`/`last_entry_bar` |
| `control_events` | nothing | every start/stop press, accepted or refused |
| `bot_snapshots` | `TradingBot.stats` | latest envelope reading, with `updated_at` |
| `deals` | nothing | raw MT5 deals, keyed on the broker's deal ticket |
| `trades` | nothing | one row per **position**, folded from `deals` |
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
  stops are checked **intrabar** against high/low; a gap fills at the **gap price**, not
  the level; SL wins same-bar ties by default (`tie_break="ambiguous"` reports how much
  edge rests on unresolvable bars); drawdown comes from the **equity** curve. Changing any
  of these invalidates every stored report in `data/reports/`.
- `backend/data/cache.py` — `BarSet.warmup_count` + `eval_slice()`. Warm-up bars are
  prepended *before* the requested range and excluded from evaluation; the engine iterates
  from `warmup_count`. This exists because the old backtest silently lost the first ~998
  bars of every window to NaN bands.
- `backend/indicators/nadaraya_watson.py` — only the **non-repainting endpoint** branch of
  the Pine source is implemented. Two traps called out in its docstring: the kernel must
  **not** be reversed for `np.convolve`, and the denominator is always the **full-window**
  weight sum even when truncated. `taps` is a speed knob, not a tunable.
- Missing data raises `DataUnavailable` naming the exact `snapshot` command to run, never
  silent NaNs.

### Warm-up arithmetic

The envelope needs `window - 1` bars for the centre line plus `mae_window` for the rolling
MAE, so the first usable index is 998 at the defaults (`nw_warmup_bars()`) and 999 closed
bars are the minimum. Falling short yields all-NaN bands, which compare `False` against
every price — the failure mode is a bot that reports "Running" and never trades. Both
paths check for this explicitly; keep it that way. Note `mae_window=500` reproduces the
original code while Pine uses 499; the off-by-one is deliberate and configurable.

### Live loop invariants (`bot_manager.py`)

Each is a fix for a real incident, marked `S1`–`S6` in comments:

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
  per bar, because the trigger is an intrabar event. It is stateless — it re-derives what
  is still to do from the position's own volume and SL, so it survives restarts. Entries
  and the mean-reversion exit remain gated per closed bar; do not move them.
- `S8`: the S4 bar marks are now **persisted** (`bot_state`). They were instance
  attributes, so Stop-then-Start — or any restart — cleared the cooldown and the
  bot could enter again on the very bar it had just entered on, which is the
  repeat-fire S4 exists to prevent, reachable from the dashboard's own buttons.
  Memory stays the working copy and is written through, so a Postgres outage
  degrades to the old behaviour rather than halting a bot holding a position.

### Scale-out / break-even

At `be_trigger_pips` in profit (default: half the target), `partial_fraction` of the
position closes and the stop moves to entry. One rule, expressed as a distance and a
*fraction* in both paths — `SYMBOL_CONFIG` live, `NWConfig.be_trigger_mode` in the
strategy. Never express it as a lot count: 0.05 is 50% of the current 0.1 lot_size and
would silently become a different share of the position if the size changed. The
dashboard edits it in **lots** because that is what a trader types; `scale_out_fraction()`
is the single boundary that converts, and the resulting percentage is echoed back to the
form so a re-scale is visible rather than silent. Do not add a second conversion.

**Measured effect on cached data (central costs), and it is negative.** Gold, 2025-05
onward, 9 settings swept: every one raises win rate and lowers expectancy versus the rule
off — 45.9%→58% win rate, expectancy −0.071R→−0.117R, monotonically worse the earlier and
larger the scale-out. At the shipped 0.1 lots / 0.5 out it is −0.071R→−0.102R. The rule
clips winners while leaving straight-to-stop losers untouched. It is enabled because it
was asked for, not because the data supports it;
`--no-breakeven`, `be_trigger_mode="none"` / `partial_fraction=0` in `SYMBOL_CONFIG`,
or a scale-out of **0 lots** in the dashboard's Position sizing panel, turns it off.
Re-run the comparison before drawing any conclusion from a report that predates it.

Of the trades that do scale out, the remainder reaches the target 55% of the time,
break-even 32%, and the centre-line exit 13% — so the mean-reversion exit intercepts the
runner much less often than the band geometry suggests. Disabling `exit_at_mean` raises the
TP share to 62% and makes expectancy worse, so leave it on.

Note when reading TP averages: rule 5 fills a gapped level at the gap price, which is
correct for stop orders but optimistic for a take-profit LIMIT, where a broker fills at
the limit. It inflates the TP tail on both sides of any comparison, so it does not bias
an A/B — but do not read an average TP win as achievable.

## Safety

`POST /control` starts and stops **live trading with real money and has no
authentication**, and `POST /settings` changes the size of the orders it sends with just
as little. `BOT_HOST` defaults to `127.0.0.1` for that reason; do not change the default
or widen `BOT_ALLOWED_ORIGINS` unless asked. There is no equity-based sizing, no daily
loss cap and no margin check — `lot_size` defaults to 0.1, so gold risks ~$70 a trade (a
measured 11-12 loss streak is ~$840). The dashboard shows that dollar figure next to the
field precisely because the lot size is the only risk control there is.

`lot_size` and `partial_fraction` are the **only** two keys `POST /settings` can touch,
and the only two persisted — now to `symbol_settings` in Postgres, not
`data/settings.json`. They are persisted because silently restoring 0.1 on restart
would undo a size someone lowered on purpose. `_load_settings()` and
`repository.load_settings()` are both narrow: only those two keys, only for symbols
already in `SYMBOL_CONFIG`, only values that survive `_validated`. A row must never
be able to introduce a symbol or move a stop — and that matters *more* with a
database than it did with a file, because psql, a migration and anything else
holding the DSN can write rows the UI never could. `schema.sql` adds CHECK
constraints as a second line of defence, refusing a bad value on the way **in**;
the file store could only reject one on the way out, at the next load.

The database is published on `127.0.0.1:5432` only, for the same reason
`BOT_HOST` is loopback. `docker compose down -v` **deletes** the trade history
and the stored lot size along with the volume.

An edit is **refused while the bot holds a position**, under the same `_CONFIG_LOCK` that
`open_trade()` holds across its `order_send`. This is not politeness:
`manage_position()` decides "has the scale-out already fired?" by comparing the
position's volume against `lot_size`, so lowering the size mid-trade makes an
already-reduced position look untouched and scales it out twice. Remembering the size
per ticket instead would need exactly the cross-restart state that S7 was written to
avoid.

`XAUUSDm` is the only configured symbol. Adding another means adding a `SYMBOL_CONFIG`
entry *and* extending `SUPPORTED_SYMBOLS` in `frontend/src/BacktestPage.tsx`, and the
per-trade dollar risk does not carry over: it comes from the contract size, not the
nominal 0.1 lots. Do not add one without a backtest showing its R:R works.

## Working conventions

`Trading Bot.md` is the standing brief for this repo, and its rules govern strategy work:
establish a baseline before modifying anything, back-test every meaningful change, never
use future information, never hide losing trades, never promise profitability, and do not
implement an "improvement" the data does not support. Prefer drawdown reduction over
headline P&L.

Comments here explain *why a defect was possible*, not what the line does. When fixing a
subtle bug, follow that pattern rather than stripping it.

`AGENTS.md` holds a short subset of the commands above.
