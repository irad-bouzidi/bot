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

- **`BTCUSDm` is negative-expectancy by construction.** Its stop (700 pips = $70)
  is larger than its target (500 pips = $50), an R:R of 0.71. Under a random walk
  a fixed double barrier wins `SL/(SL+TP)` = 58.3% of the time, which is exactly
  its zero-cost break-even win rate — so the configuration has no edge before
  costs and loses the spread on every trade. **Recommend leaving it stopped**
  pending measurement on real broker data.
- **The scale-out / break-even rule reduces expectancy on every setting tested.**
  On gold from 2025-05 with central costs it lifts the win rate from 45.9% to
  ~54% while expectancy falls from -0.071R to -0.104R, because it clips the
  average winner while doing nothing for trades that run straight to the stop.
  A 3x3 sweep of trigger and size was monotonically worse than leaving it off, on
  both symbols. It is enabled because it was requested; disable it with
  `--no-breakeven`, or `partial_fraction = 0` in `SYMBOL_CONFIG`.
- **`lot_size` is 0.1, which risks ~$70 per gold trade** against the 70-pip stop,
  with no equity-based sizing, no daily or weekly loss cap and no margin check.
  This configuration produced runs of 11-12 consecutive losses in backtest — about
  $840 — at roughly 4.4 trades per day. At the same "0.1 lots" BTC risks ~$7, a
  10x asymmetry, because the two symbols' contract sizes differ by 100x.
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
resolves roughly 90% of trades and the mean-reversion exit only ~10%; on BTC the
band half-width (median ≈ $269) is around five times the $50 target, so the mean
exit almost never fires and the strategy is effectively a fixed-barrier scalp.
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
| Backtests, baseline reports, sweeps | The live bot |

The handoff is the `data/` directory: snapshot once on the trading host, copy it
to the dev box, and all research then runs offline and reproducibly.

Exactly two modules import `MetaTrader5` — `backend/data/mt5_source.py` (reads)
and `backend/execution/` (writes). Everything else is importable and testable
without a terminal.

```
backend/
  main.py                       FastAPI app: /stats, /control, /backtest
  bot_manager.py                Live trading loop (MT5)
  core/types.py                 SymbolSpec, Signal, Side  (contract-accurate P&L)
  indicators/nadaraya_watson.py Vectorised envelope + naive reference
  strategy/nw_envelope.py       The strategy — shared by live and backtest
  data/                         MarketData abstraction, csv.gz cache, snapshot CLI
  backtest/                     Engine, cost model, trade ledger, metrics
  scripts/run_baseline.py       Baseline performance report
frontend/                       React dashboard + backtest page
tests/                          pytest suite (runs with no MT5)
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

Server settings come from the environment:

| Variable | Default | Notes |
|---|---|---|
| `BOT_HOST` | `127.0.0.1` | **Do not expose this port** — `/control` has no auth |
| `BOT_PORT` | `8000` | |
| `BOT_ALLOWED_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | CORS allowlist |

---

## 🧪 Development

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
python -m pytest                      # works without MetaTrader 5 installed
```

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
python -m backend.data.snapshot --symbol BTCUSDm --start 2023-01-01
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
3. An account carrying the symbols `XAUUSDm` and/or `BTCUSDm` — check your
   broker's exact suffix, since `XAUUSD` and `XAUUSDm` are different symbols.
4. Both symbols visible in MT5's **Market Watch** (right-click → Show All).
5. **Algo Trading enabled** in the terminal (the toolbar button must be green).

### Step 1 — Install

```bash
pip install -r requirements.txt
npm install --prefix frontend
```

### Step 2 — Verify the setup

```bash
python -m pytest                                     # should pass
python -m backend.data.snapshot --symbol XAUUSDm --spec-only
```

The second command confirms the terminal is reachable and prints your broker's
real contract specification — contract size, tick value, minimum stop distance,
allowed filling modes, and the server-to-UTC offset. If it fails here, the bot
will not trade either.

### Step 3 — Start the backend

```bash
python -m backend.main
```

Serves on `http://127.0.0.1:8000`. Loopback-only by design: `POST /control`
starts and stops live trading and has **no authentication**, so do not set
`BOT_HOST=0.0.0.0` on an untrusted network.

### Step 4 — Start the frontend

```bash
npm start --prefix frontend
```

Opens `http://localhost:3000`.

Or run both in one terminal:

```bash
npx concurrently "python -m backend.main" "npm start --prefix frontend"
```

### Step 5 — Test before risking capital

Point MT5 at a **demo account** and run the dashboard against it first. Confirm
that trades open and close as expected, and watch the backend log — every
rejected order now prints its retcode and the broker's comment.

### Step 6 — Start trading

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
- [ ] `lot_size` in `SYMBOL_CONFIG` is sized for your account — 0.1 risks ~$70
      per gold trade, so a 12-loss streak is ~$840
- [ ] `BTCUSDm` left stopped unless you have measured that its R:R works
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
