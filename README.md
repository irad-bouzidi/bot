# Nadaraya-Watson Envelope MT5 Trading Bot

An automated MetaTrader 5 trading bot implementing a Nadaraya-Watson envelope
mean-reversion strategy (after LuxAlgo's indicator), with a FastAPI backend, a
React dashboard, and an offline research harness for backtesting.

---

## ⚠️ Status

This bot places **real market orders**. Read this section before running it.

**Recent safety fixes (deploy these before trading):**

- The bot previously closed **any** position on its symbols, including trades you
  opened manually. It now filters by magic number.
- The API previously bound `0.0.0.0` with `allow_origins=["*"]` and no auth, so
  anyone on the network could start or stop live trading. It now binds
  `127.0.0.1` by default.
- Signals were evaluated on the *still-forming* candle and re-checked every 60s,
  so one candle could fire up to five entries. The bot now acts once per **closed**
  bar, with a cooldown between entries.
- Failed orders were silent (`order_send` can return `None`). Rejections now log
  the retcode, comment and `last_error()`.

**Known open issues:**

- **The scale-out / break-even rule reduces expectancy on every setting tested.**
  On gold from 2025-05 with central costs it lifts the win rate from 45.9% to
  ~54% while expectancy falls from -0.071R to -0.104R, because it clips the
  average winner while doing nothing for trades that run straight to the stop.
  A 3x3 sweep of trigger and size was monotonically worse than leaving it off.
  It is enabled because it was requested; disable it with
  `--no-breakeven`, `partial_fraction = 0` in `SYMBOL_CONFIG`, or a scale-out of
  0 lots in the dashboard's Position sizing panel.
- **`lot_size` defaults to 0.1, which risks ~$70 per gold trade** against the 70-pip
  stop. It is editable from the dashboard — which shows the dollar risk as you type —
  and persisted, but it still comes with no equity-based sizing, no daily or weekly
  loss cap and no margin check.
  This configuration produced runs of 11-12 consecutive losses in backtest — about
  $840 — at roughly 4.4 trades per day. Note that "0.1 lots" means a completely
  different dollar risk on another instrument: the risk is set by the contract
  size, not the nominal volume.
- The backtest engine has no ruin or margin model, so a simulated balance can go
  negative and drawdown can exceed 100%.

---

## 📈 Strategy

A non-repainting Gaussian kernel estimator produces a smoothed mean of price
(`out`), and an envelope is built around it from the Mean Absolute Error of price
against that mean, scaled by `MULT`.

- **Long entry** — the closed bar's price is below the lower band.
- **Short entry** — the closed bar's price is above the upper band.
- **Exit** — price returns to the centre line, **or** the broker-side fixed
  SL/TP triggers first.
- **Scale-out** — at half the target in profit, `partial_fraction` of the
  position is closed and the stop is pulled to entry, so the remainder runs at
  no risk. Measured on cached data this *lowers* expectancy (see below); it is
  configuration, not a recommendation.

**What actually happens in practice matters here.** On gold the fixed SL/TP
resolves roughly 90% of trades and the mean-reversion exit only ~10% — so the
strategy is closer to a fixed-barrier scalp than the band geometry suggests.
Measure this on your own data before assuming the described exit is the operative
one — the backtest reports an exit-reason census for exactly this purpose.

The implementation is the *endpoint* (non-repainting) branch of the Pine source,
verified equal to the original explicit loop to `9.1e-13`.

---

## 🏗 Architecture

The project assumes **two machines**. MetaTrader 5 is Windows-only and needs a
logged-in terminal, but nothing else does.

| Runs on the **dev box** (no MT5) | Runs on the **MT5 host** |
|---|---|
| All code, the full test suite | `snapshot` CLI → produces `data/` |
| Backtests, baseline reports, sweeps | The live bot + the API |

The handoff is the `data/` directory: snapshot once on the trading host, copy it
to the dev box, and all research then runs offline and reproducibly.

Two of the three pieces are **containerised** — the dashboard and Postgres
(`docker-compose.yml`, at the repo root). The backend is not, and cannot be: it
imports `MetaTrader5`, which needs a logged-in Windows terminal. Both containers
publish to `127.0.0.1` only, and the dashboard container serves static files
without proxying the API, so the API stays on loopback — see
[Containers](#-containers) below.

**Postgres holds everything the UI can change, and every trade.** Position
sizing (previously `data/settings.json`), the trade history folded from MT5's own
deals, every backtest run with its inputs, bot run state, and the dashboard's
theme and form values (previously `localStorage`). The bar cache stays in
`data/*.csv.gz`: the research stack must keep running with no database at all.

Exactly two modules import `MetaTrader5` — `backend/data/mt5_source.py` (reads)
and `backend/execution/` (writes). Everything else is importable and testable
without a terminal.

```
backend/
  main.py                       FastAPI app: /stats /control /settings /backtest
                                /trades /backtests /preferences /health /equity
  bot_manager.py                Live trading loop (MT5)
  core/types.py                 SymbolSpec, Signal, Side  (contract-accurate P&L)
  db/                           Postgres: pool, schema.sql, repository, migrate CLI
  indicators/nadaraya_watson.py Vectorised envelope + naive reference
  strategy/nw_envelope.py       The strategy — shared by live and backtest
  data/                         MarketData abstraction, csv.gz cache, snapshot CLI
  backtest/                     Engine, cost model, trade ledger, metrics
  scripts/run_baseline.py       Baseline performance report
frontend/src/
  App.tsx                       Dashboard
  BacktestPage.tsx              Backtester + stored run history
  TradesPage.tsx                Persisted trade history
  api.ts                        The only place a URL is known
  usePreferences.ts             Dashboard state, persisted to Postgres
docker-compose.yml              frontend + db (postgres) compose stack
frontend/Dockerfile             CRA build → static `serve` (no nginx)
frontend/serve.json             SPA fallback, cache rules, security headers
frontend/docker-entrypoint.sh   writes env.js from BOT_API_BASE at start
tests/                          pytest suite (runs with no MT5 and no Postgres)
data/                           Bar cache + reports (gitignored)
```

---

## ⚙️ Configuration

Strategy constants live at the top of `backend/bot_manager.py`:

| Name | Default | Meaning |
|---|---|---|
| `BANDWIDTH` | `8.0` | Kernel smoothness. Effective width ≈ 10.5 bars |
| `MULT` | `3.0` | Envelope width multiplier |
| `WINDOW_SIZE` | `500` | Kernel window |
| `MAE_WINDOW` | `500` | Band lookback (Pine uses 499) |
| `COOLDOWN_BARS` | `3` | Minimum closed bars between entries |
| `be_trigger_pips` | `50` / `250` | Profit distance that arms the scale-out (half the target) |
| `partial_fraction` | `0.5` | Proportion closed at that trigger; the rest runs to TP |
| `DEVIATION_POINTS` | `20` | Max slippage tolerated on a market order |
| `MAGIC_NUMBER` | `123456` | Identifies this bot's positions |
| `TIMEFRAME` | `M5` | Chart timeframe |

Per-symbol settings are in `SYMBOL_CONFIG` in the same file (`lot_size`,
`sl_pips`, `tp_pips`, `pip`).

`lot_size` and `partial_fraction` are also editable at runtime from the dashboard's
**Position sizing** panel and from the Backtest page, and are persisted to the
`symbol_settings` table so a restart does not quietly restore a size you lowered. Every
other key is code-only. The panel takes **lots** (0.1 and 0.05); the scale-out is stored
as the resulting share of the position, so it keeps meaning "half" if you later change
the lot size. Edits are refused while the bot holds a position — and refused, rather
than applied in memory, if the database will not accept them. Every change is
appended to `settings_audit`, which the dashboard shows under **Show sizing history**.

Server settings come from the environment:

| Variable | Default | Notes |
|---|---|---|
| `BOT_HOST` | `127.0.0.1` | **Do not expose this port** — `/control` and `/settings` have no auth |
| `BOT_PORT` | `8000` | |
| `BOT_ALLOWED_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | CORS allowlist |
| `BOT_DATABASE_URL` | `postgresql://bot:bot@127.0.0.1:5432/tradingbot` | Matches `docker-compose.yml` |
| `BOT_AUTO_RESUME` | `0` | Restart bots that were running before a shutdown. **Off by default** — see below |
| `BOT_ACCOUNT_SNAPSHOT_SECONDS` | `60` | How stale a stored account reading may be before it is re-captured |
| `BOT_SETTINGS_FILE` | `data/settings.json` | **No longer read.** Only `backend.db.migrate` uses it, to import once |

Container settings live in `.env` at the repo root (copy `.env.example`):
`POSTGRES_PASSWORD`, `POSTGRES_PORT`, `FRONTEND_PORT`, and `BOT_API_BASE` — the
API URL injected into the page at container start.

### Auto-resume is off by default

`bot_state.desired_state` records what you last asked for, so after a restart the
dashboard can tell you a bot *was* running and offer to start it again. It does
not start it for you: bringing live trading back up with real money because a
process restarted is not a decision an unauthenticated API should make on its
own. `/stats` returns the live thread status and the desired state separately —
collapsing them into one word is how a dead bot comes to report "Running".

### What is stored

| Table | Holds |
|---|---|
| `symbol_settings` + `settings_audit` | position sizing, and every change to it |
| `deals` / `trades` | MT5's raw deals, and one row per **position** folded from them |
| `bot_state` / `control_events` | desired run state, the per-bar entry guards, every start/stop press |
| `bot_snapshots` | the latest envelope reading per symbol |
| `backtest_runs` | every backtest, inputs and outputs, failures included |
| `ui_preferences` | theme, active view, backtest form values |
| `account_snapshots` | throttled account readings, which accumulate an equity curve |

Trade statistics are **queries** over `trades`, never counters — so a restart, a
double poll, or a re-scanned history window cannot make them drift. One trade is
one outcome, decided on net profit: a trade that banks a scale-out and then stops
at break-even is one row with `exit_count = 2`, not a win plus a flat.

---

## 📦 Containers

Two services, both in `docker-compose.yml` at the repo root. There is no
`docker/` directory and no reverse proxy.

```bash
cp .env.example .env          # optional; the defaults work
docker compose up -d
python -m backend.db.migrate  # from the repo root, once
python -m backend.main        # on the MT5 host, as before
```

| | Runs where | Published on |
|---|---|---|
| `db` (postgres:16-alpine) | container | `127.0.0.1:5432` |
| `frontend` (node + `serve`) | container | `127.0.0.1:3000` |
| API (`python -m backend.main`) | **host** — needs MT5 | `127.0.0.1:8000` |

The backend is not here and cannot be: `backend/bot_manager.py` imports
`MetaTrader5`, which is Windows-only and needs a logged-in terminal on the same
machine. Containerising it would mean shipping a Windows image with a GUI
trading terminal inside.

### Why nothing proxies the API

The browser loads the page from the frontend container on `:3000` and calls the
API **directly** on `127.0.0.1:8000`. That container serves static files and
nothing else — no `/api` route, no proxy, no nginx.

A proxy would be the conventional layout, and it is the wrong one here. For the
container to reach the API, the API would have to be reachable from the Docker
bridge network — i.e. bound off `127.0.0.1` — and `POST /control` starts live
trading with real money with no authentication at all, as does `POST /settings`
for the size of the orders it sends. The loopback bind is the only thing
protecting either. Adding the container's published origin to the backend's CORS
list costs nothing by comparison, and `BOT_ALLOWED_ORIGINS` already defaults to
including `:3000`.

The cost is that the dashboard only works from a browser on the MT5 host — which
is the constraint the loopback bind already imposed.

Both published ports are bound to `127.0.0.1` for the same reason.
`- "5432:5432"` would bind `0.0.0.0` and put the database holding `lot_size` on
every interface behind a password that defaults to `bot`.

### What the frontend container is

`frontend/Dockerfile` builds the CRA bundle on `node:24-alpine` and serves it
from a second `node:24-alpine` stage with `serve`, as the unprivileged `node`
user on `:3000`. `frontend/serve.json` carries what an nginx config used to:

| | |
|---|---|
| SPA fallback | unknown paths are client routes, so they resolve to `index.html` |
| `static/**` | `public, max-age=31536000, immutable` — CRA hashes those filenames |
| `index.html`, `env.js` | `no-store` — see below |
| every response | `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer` |

The page holds the start/stop controls for a live account, so nothing about it
should be embeddable or content-sniffable. Unlike nginx's `add_header` — which
*replaces* the inherited set rather than merging, so a server-level header
silently reached no location that set its own `Cache-Control` — matching
`headers` entries here merge, and the `**` rule genuinely applies to everything.

`serve.json` is kept **outside** the served directory and passed with
`--config`; `serve` would otherwise look for it at `<public>/serve.json` and also
hand it out at `/serve.json`.

### Why the API base is injected at runtime

Create React App inlines every `REACT_APP_*` value into the bundle at build
time, so an API URL chosen at build time can only be changed by rebuilding the
image. Instead `frontend/docker-entrypoint.sh` writes `env.js` from
`BOT_API_BASE` on every container start and `exec`s the server, `index.html`
loads it before the bundle, and `frontend/src/api.ts` reads
`window.__BOT_CONFIG__.apiBase`. `frontend/public/env.js` is the dev default for
`npm start`.

`env.js` and `index.html` are served `no-store`: a cached `env.js` would pin the
dashboard to a previous `BOT_API_BASE` and it would silently poll the wrong host.

The literal is written with node's `JSON.stringify`, not by escaping characters
by hand. The previous version used `sed 's/\\/\\\\/g; s/"/\\"/g'`, which
busybox sed rejects outright — *"bad option in substitution expression"* — so
`env.js` was never actually rewritten and `BOT_API_BASE` did nothing. It only
looked like it worked because the dev default happens to be the same URL.

### The schema

`docker-compose.yml` mounts `./backend/db/schema.sql` into
`/docker-entrypoint-initdb.d`, so a **fresh** database gets the schema on first
boot. It is mounted rather than copied, because a copy would drift from the real
file the first time a column changed.

Postgres ignores that directory once the data volume exists, so
`python -m backend.db.migrate` is the path that works every time. It is
idempotent — every statement in `schema.sql` is `IF NOT EXISTS` — so running
both is harmless. `migrate` also:

- imports `data/settings.json` into `symbol_settings` **once**, so a lot size you
  lowered on purpose is not lost in the move to Postgres. A re-run leaves the
  database value alone rather than restoring the file's;
- seeds a row for any configured symbol that has none, so `/settings` never has
  to answer "no row" — the fallback for which would be the 0.1 code default.

`python -m backend.db.migrate --check` reports connectivity and schema version
and changes nothing.

### Data, and the volume name

Postgres data lives in the named volume **`nw-bot-db-data`**. The compose
project name is pinned (`name: nw-bot`) and the volume is named explicitly
rather than left to the `<project>_<key>` default, because that default derives
from the *directory name* — so moving or renaming a directory silently brings
the stack up against an empty database, which is the silent restore of the 0.1
`lot_size` default that the backend refuses to boot for.

When the compose file lived in `docker/`, the volume was `docker_db-data`. Carry
it over rather than starting fresh:

```powershell
docker run --rm -v docker_db-data:/from:ro -v nw-bot-db-data:/to alpine sh -c 'cd /from && tar cf - . | (cd /to && tar xf -)'
```

The volume survives `docker compose down`. `docker compose down -v` **deletes
it**, including the trade history and the stored lot size.

```powershell
# back it up first
docker exec nw-bot-db pg_dump -U bot tradingbot > backup.sql
```

### Node version

Both stages are `node:24-alpine`, matching the npm major that produced
`frontend/package-lock.json` (lockfileVersion 3, npm 11). `node:20` ships npm
10, which reads the same lock file and rejects it — *"Missing: yaml@2.9.0 from
lock file"* — because the two majors resolve that transitive dependency
differently. `npm ci` is worth keeping over `npm install` for a container build,
so the image moved rather than the guarantee.

Shell scripts are pinned to LF in `.gitattributes`. With `core.autocrlf=true` on
a Windows checkout, a `#!/bin/sh
` shebang does not resolve inside a Linux
container, and the frontend image's `ENTRYPOINT` is a shell script — so that is
not a warning in a log, it is a container that cannot start.

---

## 🧪 Development

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
python -m pytest                      # no MetaTrader 5 and no Postgres needed
npm test --prefix frontend            # dashboard tests
```

`tests/test_db_repository.py` exercises the SQL against a real server and
**skips itself** when none answers, so the default suite stays offline. To run
it: `docker compose up -d db` then
`python -m backend.db.migrate`. It works in a throwaway schema, so it cannot
touch the lot size the bot actually trades.

The suite covers indicator parity against the original loop, a no-look-ahead
property test, warm-up handling, cost accounting, and the engine's intrabar and
gap-fill rules.

---

## 🔬 Research workflow

Backtest numbers are only as good as the execution rules behind them. This engine
fills at the **next bar's open**, checks stops **intrabar** against high/low,
fills gaps **at the gap price** rather than the stop level, and models spread,
commission, slippage and swap. Expect materially worse — and more realistic —
results than a close-only, cost-free backtest.

**1. Snapshot history (on the MT5 host):**

```bash
python -m backend.data.snapshot --symbol XAUUSDm --start 2023-01-01
python -m backend.data.snapshot --list          # coverage report
```

Copy the resulting `data/` directory to wherever you run research.

**2. Produce the baseline (anywhere, offline):**

```bash
python -m backend.scripts.run_baseline --symbol XAUUSDm --compare-legacy
```

This prints every metric, breakdowns by session / day-of-week / direction /
exit-reason, three cost scenarios, and writes the full trade ledger to
`data/reports/`. `--compare-legacy` also runs the original close-only, cost-free
engine so you can see how much it was overstating results.

---

## 🚀 Running the Bot

### Prerequisites

1. **MetaTrader 5 terminal** installed, running, and **logged into your account**.
   The bot attaches to the open terminal; it does not log in itself.
2. **Python 3.8+** and **Node.js**.
3. An account carrying the symbol `XAUUSDm` — check your broker's exact suffix,
   since `XAUUSD` and `XAUUSDm` are different symbols.
4. The symbol visible in MT5's **Market Watch** (right-click → Show All).
5. **Algo Trading enabled** in the terminal (the toolbar button must be green).
6. **Docker**, for Postgres and the dashboard container.

### Step 1 — Install

```bash
pip install -r requirements.txt
npm install --prefix frontend
```

### Step 2 — Start Postgres and apply the schema

```bash
docker compose up -d
python -m backend.db.migrate
```

The backend **will not start** without a reachable, migrated database, and that
is deliberate: `lot_size` lives there and it is the only risk control this bot
has, so falling back to the 0.1 default because the database was down would
quietly restore ~$70/trade for someone who had lowered it on purpose.

`migrate` imports an existing `data/settings.json` **once** so a size you chose
is carried over, then leaves the database value alone on every later run.
`python -m backend.db.migrate --check` reports status without changing anything.

### Step 3 — Verify the setup

```bash
python -m pytest                                     # should pass
python -m backend.db.migrate --check                 # schema version 1
python -m backend.data.snapshot --symbol XAUUSDm --spec-only
```

The second command confirms the terminal is reachable and prints your broker's
real contract specification — contract size, tick value, minimum stop distance,
allowed filling modes, and the server-to-UTC offset. If it fails here, the bot
will not trade either.

### Step 4 — Start the backend

```bash
python -m backend.main
```

Serves on `http://127.0.0.1:8000`. Loopback-only by design: `POST /control`
starts and stops live trading and `POST /settings` changes its position size, both
with **no authentication**, so do not set `BOT_HOST=0.0.0.0` on an untrusted network.

### Step 5 — Open the dashboard

The container from Step 2 already serves it on `http://localhost:3000`. For
development with hot reload, use the dev server instead:

```bash
npm start --prefix frontend
```

Either way the **browser** calls the API on `127.0.0.1:8000` directly; nothing
proxies it. That is what lets the API stay bound to loopback — see
[Containers](#-containers).

Or run backend and dev server together:

```bash
npx concurrently "python -m backend.main" "npm start --prefix frontend"
```

### Step 6 — Test before risking capital

Point MT5 at a **demo account** and run the dashboard against it first. Confirm
that trades open and close as expected, and watch the backend log — every
rejected order now prints its retcode and the broker's comment.

### Step 7 — Start trading

Press **Start** on the dashboard for the symbol you want. The bot will:

1. Fetch 1,200 M5 bars and discard the still-forming one.
2. Compute the envelope; if fewer than 999 closed bars are available it logs a
   warning and does **not** trade rather than failing silently.
3. Act at most once per closed bar, respecting the entry cooldown.
4. Attach a broker-side SL and TP, rounded to the symbol's tick size and widened
   if they fall inside the broker's minimum stop distance.

Press **Stop** to halt. The bot finishes its current cycle within about a second;
**open positions are left open** — close them in MT5 yourself if you want flat.

### Before going live — checklist

- [ ] Ran on a demo account first
- [ ] `BOT_HOST` is `127.0.0.1`
- [ ] `lot_size` is sized for your account — 0.1 risks ~$70 per gold trade, so a
      12-loss streak is ~$840. Check it in the dashboard's Position sizing panel,
      which shows the dollar risk; `data/settings.json` may hold a value that
      differs from the `SYMBOL_CONFIG` default
- [ ] You know the bot has no daily loss limit — monitor it
- [ ] Backend log is visible; it is where order rejections appear

---

## ⚠️ Disclaimer

Trading financial instruments carries significant risk of loss. This software is
provided for educational and research purposes with no warranty and no
representation that it is profitable. Historical backtest results do not predict
future returns. Always test on a demo account first, and never risk capital you
cannot afford to lose.

---

*Strategy based on LuxAlgo — Nadaraya-Watson Envelope and Smoothers.*
