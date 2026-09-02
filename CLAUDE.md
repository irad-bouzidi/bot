# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
pip install -r requirements.txt          # runtime (includes MetaTrader5, Windows-only)
pip install -r requirements-dev.txt      # pytest, pytest-cov
npm install --prefix frontend

# Tests -- must be run from the repo root (tests/conftest.py inserts it on sys.path).
# The whole suite passes with no MetaTrader 5 terminal and no `data/` directory.
python -m pytest
python -m pytest tests/test_backtest_engine.py::test_intrabar_stop_is_detected_even_when_close_recovers
python -m pytest -k lookahead
npm test --prefix frontend               # CRA/jest; only App.test.tsx exists

# Run
python -m backend.main                   # FastAPI on 127.0.0.1:8000 (needs MT5 running)
npm start --prefix frontend              # dashboard on localhost:3000
npx concurrently "python -m backend.main" "npm start --prefix frontend"

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

## Architecture

The project assumes **two machines**: MT5 is Windows-only and needs a logged-in terminal;
nothing else does. `data/` (gitignored, but populated here) is the handoff — snapshot on
the trading host, copy it over, and all research runs offline and reproducibly.

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
| Backtest | `BotManager.run_backtest` — close-only, cost-free, **no scale-out** | `backend/backtest/engine.py` — next-bar-open fills, intrabar stops, cost model |
| Data | `mt5.copy_rates_*` direct | `MarketData` / `CachedMarketData` over `data/` |
| P&L | `price_diff * lot_size * profit_mult` | `SymbolSpec.pl()` from real tick value |
| Config | `SYMBOL_CONFIG` dict, pips | `NWConfig` + `BacktestConfig`, price units |

`POST /backtest` (used by the frontend Backtest page) still runs the **legacy** engine, so
its numbers are systematically optimistic and do not match `run_baseline`. The research
stack is the honest one; `BacktestConfig(legacy_mode=True)` reproduces the old behaviour
inside the new engine purely for regression comparison (`run_baseline --compare-legacy`
prints the overstatement). When changing strategy behaviour, expect to change it in
**both** places, or say clearly that you did not.

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

### Scale-out / break-even

At `be_trigger_pips` in profit (default: half the target), `partial_fraction` of the
position closes and the stop moves to entry. One rule, expressed as a distance and a
*fraction* in both paths — `SYMBOL_CONFIG` live, `NWConfig.be_trigger_mode` in the
strategy. Never express it as a lot count: 0.05 is 50% of the current 0.1 lot_size and
would silently become a different share of the position if the size changed.

**Measured effect on cached data (central costs), and it is negative.** Gold, 2025-05
onward, 9 settings swept: every one raises win rate and lowers expectancy versus the rule
off — 45.9%→58% win rate, expectancy −0.071R→−0.117R, monotonically worse the earlier and
larger the scale-out. At the shipped 0.1 lots / 0.5 out it is −0.071R→−0.102R. The rule
clips winners while leaving straight-to-stop losers untouched. It is enabled because it
was asked for, not because the data supports it;
`--no-breakeven`, or `be_trigger_mode="none"` / `partial_fraction=0` in `SYMBOL_CONFIG`,
turns it off. Re-run the comparison before drawing any conclusion from a report that
predates it.

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
authentication**. `BOT_HOST` defaults to `127.0.0.1` for that reason; do not change the
default or widen `BOT_ALLOWED_ORIGINS` unless asked. There is no equity-based sizing, no
daily loss cap and no margin check — `lot_size` is a flat 0.1 in `SYMBOL_CONFIG`, so gold
risks ~$70 a trade (a measured 11-12 loss streak is ~$840).

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
