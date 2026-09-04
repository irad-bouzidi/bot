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
python -m pytest tests/test_db_repository.py   # needs the db container up (32 tests)
npm test --prefix frontend               # CRA/jest; App.test.tsx (17 tests)

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
# --sl/--tp are PRICE units, SYMBOL_CONFIG is pip COUNTS times a per-symbol pip.
# They now DEFAULT from backend/core/symbols.py -- gold 70x0.1 -> 7/10, Bitcoin
# 700x1.0 -> 700/1000 -- and the chosen numbers are printed at the top of the
# report. Passing 7/10 for BTCUSDm would put a $7 stop on an $81,000 instrument.

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
nothing else does. `data/` (gitignored, but populated here) is the handoff — snapshot on
the trading host, copy it over, and all research runs offline and reproducibly.

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
| Config | `SYMBOL_CONFIG` (`backend/core/symbols.py`), pip counts; sizing from Postgres | `NWConfig` + `BacktestConfig`, price units |
| Storage | Postgres (`backend/db/`) | `data/` files only — never Postgres |
| Sizing | `SYMBOL_CONFIG["lot_size"]` / `"partial_fraction"`, editable via `POST /settings` | `BacktestConfig.volume` / `NWConfig.partial_fraction`, CLI flags |
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
| `symbol_settings` | `data/settings.json` | the three `EDITABLE_KEYS` only; CHECK constraints refuse a bad value on the way *in*, which a file could not. `exit_at_mean` is BOOLEAN and needs none -- the type is the constraint |
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

Each is a fix for a real incident, marked `S1`–`S10` in comments. **`S9` is retired**
— it was the news blackout, now removed — and the number is deliberately left as a gap
rather than reused, so an `S9` in an older comment or commit still means what it said:

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
- `S10`: the centre-line exit lives in `_mean_reversion_exit()`, extracted from `run()`
  rather than inlined. Two reasons. It reads `exit_at_mean` under `_CONFIG_LOCK` — unlike
  `pip` in `manage_position()`, this key is editable at runtime *and* editable while a
  position is open, so the loop can genuinely race a save. And nothing in the suite drives
  `run()`, so for as long as it was six inlined lines, the rule deciding most of this
  strategy's exits had no test at all.

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

**Measured A/B**, M5, 2025-09-01 → 2026-09-04, 0.1 lots on $1,000, central costs,
scale-out on. The ON column reproduces the stored baseline table below to every printed
digit, which is what confirms the flag is a true no-op when enabled:

| | XAU off | XAU on | BTC off | BTC on |
|---|---|---|---|---|
| closed trades | 1,761 | 1,761 | 1,256 | 1,701 |
| win rate | 53.15% | 53.44% | 57.25% | 61.67% |
| net P&L | −$14,076 | −$13,046 | −$2,952 | −$3,677 |
| profit factor | 0.823 | 0.834 | 0.924 | 0.890 |
| expectancy | −0.114 R | −0.106 R | −0.034 R | −0.031 R |
| max drawdown | 1001% | 908% | 321% | 356% |
| `cross_center` exits | 0 | 113 (6.4%) | 0 | 1,190 (70%) |

**The direction is split by symbol, and the shipped default is not the better one on gold.**
Off costs gold ~$1,030 and 93 points of drawdown; it gains Bitcoin ~$725, 34 points of
profit factor and 35 points of drawdown. Read the BTC trade counts carefully — 1,256 against
1,701 — because without the early exit positions are held longer and fewer complete inside
the same window, which is also why BTC's per-trade expectancy is marginally *worse* off
(−0.034 R) while its net P&L and drawdown are better. It ships off on both because one rule
across both instruments was asked for, not because gold's column supports it. Both symbols
remain losing configurations either way.

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
or widen `BOT_ALLOWED_ORIGINS` unless asked. There is no equity-based sizing, no daily
loss cap and no margin check — `lot_size` defaults to 0.1, so gold risks ~$70 a trade (a
measured 11-12 loss streak is ~$840). The dashboard shows that dollar figure next to the
field precisely because the lot size is the only risk control there is.

`lot_size`, `partial_fraction` and `exit_at_mean` are the **only** three keys
`POST /settings` can touch, and the only three persisted — now to `symbol_settings`
in Postgres, not `data/settings.json`. They are persisted because silently restoring
0.1 on restart would undo a size someone lowered on purpose, and restoring the
centre-line exit would undo a rule someone switched off on purpose.
`_load_settings()` and `repository.load_settings()` are both narrow: only those keys,
only for symbols already in `SYMBOL_CONFIG`, only values that survive `_validated`.
A row must never be able to introduce a symbol or move a stop — and that matters
*more* with a database than it did with a file, because psql, a migration and
anything else holding the DSN can write rows the UI never could. `schema.sql` adds
CHECK constraints as a second line of defence, refusing a bad value on the way
**in**; the file store could only reject one on the way out, at the next load.

Three things to keep straight about the third key, since it is the first non-float one:

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

A `POST /settings` carrying only `exit_at_mean` is **accepted while a position is
open**, unlike a sizing edit. The sizing refusal exists because `manage_position()`
infers "has the scale-out fired?" from the position's volume against `lot_size`;
this flag takes part in no such inference, and the moment someone reaches for it is
while a trade is running and the centre line is closing in on it. Refusing it then
would withhold the control in the only situation that motivates it.

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

An edit is **refused while the bot holds a position**, under the same `_CONFIG_LOCK` that
`open_trade()` holds across its `order_send`. This is not politeness:
`manage_position()` decides "has the scale-out already fired?" by comparing the
position's volume against `lot_size`, so lowering the size mid-trade makes an
already-reduced position look untouched and scales it out twice. Remembering the size
per ticket instead would need exactly the cross-restart state that S7 was written to
avoid.

### Symbols

`XAUUSDm` and `BTCUSDm` are configured, both in **`backend/core/symbols.py`** --
`SYMBOL_CONFIG` moved there out of `bot_manager` so that `backend.db.migrate` (no
terminal) and `run_baseline` (no terminal, no database) can read the same table
instead of copying it. `bot_manager` re-exports the same dict object, so a running bot
still holds a live reference into it and a size edit still reaches the thread.

| | XAUUSDm | BTCUSDm |
|---|---|---|
| `pip` | 0.1 | 1.0 |
| stop / target | 70 / 100 pips = 7.00 / 10.00 | 700 / 1000 pips = 700 / 1000 |
| scale-out trigger | 50 pips = 5.00 | 500 pips = 500 |
| `profit_mult` (contract size) | 100 oz per lot | 1 BTC per lot |
| risk at the 0.1 default | ~$70 | ~$70 |
| `exit_at_mean` | `False` (editable) | `False` (editable) |

The pip COUNTS are identical on purpose -- one rule, two instruments -- so the worked
example reads the same on both: a BTCUSDm long at 80500 targets 81500, stops at 79800,
and at 81000 banks `partial_fraction` of the position and pulls the stop to 80500.

**The equal $70 is a coincidence of the two contract sizes, not a rule.** Gold is 100 oz
over a 7.00 stop, Bitcoin 1 BTC over a 700.00 one. A third symbol will land wherever its
contract size puts it, so re-derive the dollar risk rather than assuming 0.1 lots means
$70. `TradingBot.__init__` and `run_backtest` now **refuse** an unconfigured symbol
instead of falling back to gold's row, because that fallback is silent and gold's pip
would compute a $0.70 stop on an $81,000 instrument.

Adding a third symbol means a `SYMBOL_CONFIG` entry and nothing else in the frontend --
the Backtest page reads its symbol list from `/settings`, which is keyed off
`SYMBOL_CONFIG`. Still do not add one without a backtest showing its R:R works.

**BTCUSDm's measured baseline is NEGATIVE**, like gold's. Central costs, M5, 2025-09-01
to 2026-09-04, 0.1 lots on $1,000, honest engine (`data/reports/BTCUSDm_20260904_*`):

| | XAUUSDm 7/10 | BTCUSDm 700/1000 |
|---|---|---|
| trades | 1,761 | 1,701 |
| win rate | 53.4% | 61.7% |
| net P&L | −$13,046 | −$3,677 |
| profit factor | 0.83 | 0.89 |
| expectancy | −0.106 R | −0.031 R |
| max drawdown | 908% | 356% |

Bitcoin is the *less bad* of the two on the identical window at the same nominal risk --
better profit factor, a third of the drawdown -- and it is still a losing configuration.
The scale-out hurts it exactly as it hurts gold: with `--no-breakeven` the same window is
−$2,713 at −0.023R and 275% drawdown, so the rule costs ~$960 and 80 points of drawdown.
It ships enabled because it was asked for, not because the data supports it.

**Both tables above predate the centre-line exit becoming a flag, and were produced
with it ON.** That is no longer the default, so neither column describes what
`run_baseline` now does without `--exit-at-mean` — the honest-engine figures for the
shipped configuration are the "off" columns in the A/B table under "Centre-line
(mean-reversion) exit" above (gold −$14,076 at 1001% drawdown, Bitcoin −$2,952 at 321%).
Their `exit_reason` censuses are affected too: a `signal` row in any report stored before
that change is a `cross_center`.

They are otherwise still valid, including across the removal of the news blackout: that
rule was a no-op in the research engine unless a calendar was passed, and no stored report
ever passed one. Re-running the gold command above after the removal reproduced the
ledger **byte for byte** (768,655 bytes) and every metric in all three cost scenarios;
only the five dead `news_*` keys dropped out of `<stamp>_config.json`.

## Working conventions

`Trading Bot.md` is the standing brief for this repo, and its rules govern strategy work:
establish a baseline before modifying anything, back-test every meaningful change, never
use future information, never hide losing trades, never promise profitability, and do not
implement an "improvement" the data does not support. Prefer drawdown reduction over
headline P&L.

Comments here explain *why a defect was possible*, not what the line does. When fixing a
subtle bug, follow that pattern rather than stripping it.

`AGENTS.md` holds a short subset of the commands above.
