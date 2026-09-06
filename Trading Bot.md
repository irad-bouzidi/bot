# Trading Bot — Comprehensive Review, Optimization & Profitability Enhancement

You are an expert quantitative trader, algorithmic trading engineer, and Python software engineer.

I want you to **deeply review my existing trading bot in "backend/bot_manager.py" and improve it with the goal of increasing risk-adjusted profitability, trade accuracy, and profitable-trade percentage**, while avoiding overfitting and excessive risk.

Do NOT assume that adding more trades automatically improves performance. Every modification must be supported by data, backtesting, and measurable improvement.

## 1. Understand the Existing Bot

First, inspect the entire codebase and understand:

- Trading strategy and entry conditions
- Buy/sell logic
- Indicators and technical analysis
- Timeframes
- Market/session filters
- Stop-loss logic
- Take-profit logic
- Position sizing
- Risk management
- Leverage/margin handling
- Trailing stop / break-even logic
- Maximum simultaneous positions
- Daily/weekly loss limits
- News/volatility filters
- Signal confirmation logic
- Trade execution logic
- Spread/slippage handling
- Account/balance management
- Configuration/environment variables
- Logging and trade history
- Error handling and recovery
- Existing backtesting functionality
- Existing performance metrics

Before changing anything, provide a concise explanation of how the current strategy works.

---

# 2. Establish a Baseline

Before making modifications, run the existing strategy against the available historical data.

Record a baseline containing at minimum:

- Total trades
- Winning trades
- Losing trades
- Win rate (%)
- Gross profit
- Gross loss
- Net P&L
- Profit factor
- Average winning trade
- Average losing trade
- Average trade
- Maximum drawdown
- Maximum consecutive losses
- Maximum consecutive wins
- Risk/reward ratio
- Expectancy per trade
- Sharpe ratio if meaningful
- Sortino ratio if meaningful
- ROI
- Largest single win
- Largest single loss
- Trading frequency
- Long win rate
- Short win rate
- Performance by timeframe
- Performance by trading session
- Performance by day of week
- Performance by market regime
- Performance by indicator/signal type if available

Save these results so every subsequent modification can be compared against the baseline.

---

# 3. Identify Weaknesses

Analyze the strategy and determine why losing trades occur.

Look specifically for:

- Weak entry signals
- Late entries
- False breakouts
- Choppy-market entries
- Low-volume entries
- Bad risk/reward setups
- Poor stop-loss placement
- TP levels that are too ambitious
- TP levels that are too conservative
- Trading against the dominant trend
- Trading during unfavorable sessions
- Excessive trading
- Repeated entries on the same signal
- Correlated positions
- Poor volatility adaptation
- Spread-sensitive trades
- Slippage-sensitive trades
- News-driven volatility
- Over-reliance on a single indicator
- Conflicting indicators
- Look-ahead bias
- Data leakage
- Overfitting
- Unrealistic backtesting assumptions

Do not simply add indicators.

Determine which components actually contribute predictive value.

---

# 4. Improve Signal Quality

Evaluate whether the bot would benefit from stronger confirmation logic.

Consider, where appropriate:

- Trend detection
- Market structure
- Support/resistance
- Breakout confirmation
- Pullback confirmation
- Momentum
- Volume
- Volatility
- ATR
- RSI
- MACD
- Moving averages
- VWAP
- ADX
- Higher-timeframe confirmation
- Candlestick/price-action confirmation
- Liquidity conditions
- Spread filtering

Do not add an indicator unless testing demonstrates that it improves out-of-sample performance.

Prefer a smaller number of robust signals over a large collection of correlated indicators.

---

# 5. Improve Entry Logic

Evaluate multiple entry approaches.

For example:

- Trend-following entries
- Pullback entries
- Breakout entries
- Momentum entries
- Mean-reversion entries

Test whether requiring multiple independent confirmations improves:

- Win rate
- Expectancy
- Profit factor
- Drawdown

Do not optimize exclusively for win rate.

A strategy with a 45% win rate and excellent risk/reward can be much more profitable than one with a 75% win rate and poor risk/reward.

---

# 6. Optimize Stop Loss & Take Profit

Analyze the existing SL/TP system.

Test alternatives such as:

- Fixed SL/TP
- ATR-based SL
- ATR-based TP
- Market-structure SL
- Dynamic risk/reward
- Partial take profit
- Break-even
- Trailing stop
- Volatility-adjusted exits
- Time-based exits

Test several risk/reward configurations.

For example:

- 1:1
- 1:1.25
- 1:1.5
- 1:2
- 1:2.5
- 1:3

Do NOT select the configuration simply because it produces the highest historical profit.

Prefer configurations that remain profitable across different periods and market conditions.

---

# 7. Dynamic Position Sizing

Review the current position sizing.

If appropriate, implement risk-based sizing where the position size is determined by:

- Account equity
- Stop-loss distance
- Maximum percentage risk per trade
- Instrument characteristics
- Volatility

Never increase position size merely to increase P&L.

The objective is:

**maximize long-term risk-adjusted return while controlling drawdown.**

Implement safeguards such as:

- Maximum risk per trade
- Maximum daily loss
- Maximum weekly loss
- Maximum open exposure
- Maximum correlated exposure
- Maximum consecutive-loss protection

---

# 8. Market Regime Detection

Determine whether the strategy behaves differently during:

- Strong uptrends
- Strong downtrends
- Sideways markets
- High volatility
- Low volatility
- High-volume periods
- Low-volume periods

If performance is significantly worse in certain regimes, consider filtering those conditions instead of forcing the bot to trade continuously.

---

# 9. Session Optimization

Analyze performance by trading session.

For example:

- Asian
- London
- New York
- London/New York overlap

Determine:

- Win rate
- P&L
- Profit factor
- Drawdown
- Average trade

for each session.

Disable or reduce trading during consistently unprofitable conditions only if the improvement survives out-of-sample testing.

---

# 10. Long vs Short Analysis

Analyze BUY and SELL trades separately.

Determine:

- BUY win rate
- SELL win rate
- BUY P&L
- SELL P&L
- BUY profit factor
- SELL profit factor
- BUY drawdown
- SELL drawdown

If one direction consistently performs worse, investigate why before simply disabling it.

---

# 11. Avoid Overfitting

This is extremely important.

Do NOT optimize parameters until historical backtests look perfect.

Use:

- Train/test separation
- Walk-forward testing
- Out-of-sample validation
- Multiple market periods
- Different volatility regimes
- Parameter sensitivity analysis

A parameter should preferably work across a reasonable range rather than only at one exact value.

Example:

Bad:

`RSI = 53` produces excellent results while 52 and 54 perform poorly.

Better:

`RSI between 50–55` performs consistently well.

Prefer robust parameter regions.

---

# 12. Include Realistic Trading Costs

Backtesting must account for:

- Spread
- Commission
- Slippage
- Swap/overnight costs where applicable
- Execution latency where relevant

Do not consider a strategy profitable if the edge disappears after realistic trading costs.

---

# 13. Optimize for the Right Objective

Do NOT optimize solely for:

- Maximum profit
- Maximum win rate
- Maximum number of winning trades

Instead, evaluate a combined objective including:

- Net P&L
- Profit factor
- Win rate
- Expectancy
- Maximum drawdown
- Risk-adjusted return
- Trade count
- Stability across periods

A useful conceptual objective is:

**Quality Score = profitability + expectancy + consistency − drawdown − instability**

You may design a more rigorous scoring function if appropriate.

---

# 14. Trade Filtering

Investigate whether low-quality trades can be removed using:

- Minimum signal strength
- Minimum trend strength
- Volatility threshold
- Spread threshold
- Higher-timeframe trend alignment
- Session filtering
- News filtering
- Minimum risk/reward
- Market-regime filtering

Measure the effect of every filter independently.

Avoid creating so many filters that the strategy becomes overfit.

---

# 15. Logging & Explainability

Improve the bot so every trade records the reasons behind the decision.

For each trade, log:

- Timestamp
- Symbol
- Direction
- Entry
- SL
- TP
- Position size
- Market regime
- Volatility
- Spread
- Indicator values
- Signal strength
- Entry reasons
- Exit reason
- P&L
- MAE
- MFE
- Duration

This will allow future analysis of which conditions produce profitable trades.

---

# 16. Compare Every Change

For every proposed modification, produce something similar to:

| Metric | Baseline | New | Change |
|---|---:|---:|---:|
| Win Rate | X% | Y% | +Z% |
| Net P&L | X | Y | +Z |
| Profit Factor | X | Y | +Z |
| Max Drawdown | X | Y | -Z |
| Expectancy | X | Y | +Z |
| Trades | X | Y | +/-Z |

Reject changes that improve one metric while materially damaging overall strategy quality.

---

# 17. Preserve Safety

Never implement logic designed to artificially hide losses.

Do NOT use:

- Unlimited martingale
- Unlimited averaging down
- Loss chasing
- Increasing leverage after losses
- Removing stop losses
- Manipulating backtest results
- Look-ahead data
- Future candles
- Data leakage

If the current bot contains dangerous behavior, explicitly identify it and recommend safer alternatives.

---

# 18. Code Quality

While improving the strategy:

- Keep the code maintainable
- Separate strategy logic from execution
- Separate indicators from signal generation
- Separate risk management from strategy logic
- Avoid duplicated logic
- Add configuration parameters where appropriate
- Add unit tests
- Add backtesting tests
- Preserve existing functionality unless there is a reason to change it
- Do not introduce unnecessary dependencies

---

# 19. Final Optimization Report

After implementing improvements, provide a final report containing:

### Strategy Changes
Explain every meaningful change.

### Performance Comparison

| Metric | Original | Improved | Difference |
|---|---:|---:|---:|
| Win Rate | | | |
| Net P&L | | | |
| Profit Factor | | | |
| Expectancy | | | |
| Max Drawdown | | | |
| ROI | | | |
| Number of Trades | | | |

### Robustness

Report:

- In-sample performance
- Out-of-sample performance
- Walk-forward performance
- Best period
- Worst period
- Different market regimes
- Parameter sensitivity

### Remaining Weaknesses

Explain where the strategy still performs poorly.

### Recommended Production Configuration

Provide the final recommended configuration and explain why.

---

# Critical Rules

1. **Do not promise profitability.**
2. **Do not optimize only for win rate.**
3. **Do not optimize only for historical P&L.**
4. **Do not overfit.**
5. **Do not use future information.**
6. **Do not hide losing trades.**
7. **Do not increase risk simply to increase P&L.**
8. **Every meaningful strategy change must be backtested.**
9. **Prefer robust improvements that work across multiple market conditions.**
10. **If the data does not support an improvement, do not implement it.**
11. **If an optimization reduces drawdown while maintaining profitability, consider it a valuable improvement even if total P&L increases less.**
12. **The ultimate objective is sustainable positive expectancy and risk-adjusted profitability, not an artificially high historical win rate.**

Start by analyzing the existing codebase and producing the **baseline performance report before modifying the strategy**.
---
---

# ANSWERS — where this brief stands, as of 2026-09-06

*Everything above is the brief and is left exactly as written. Everything below is the
response to it. Numbers here are reproducible from the committed reports named beside
them; the working notes behind each finding are in `CLAUDE.md`.*

**The headline, stated first because the rest of this document is the evidence for it:
this strategy has no measurable edge on either configured instrument, the reason is not
a tuning problem, and no configuration found so far survives out-of-sample testing.
Per critical rule 10, nothing was shipped to "fix" that.** What *was* built is the
measurement apparatus that makes the claim checkable — and one execution bug whose
removal invalidated every number this project had previously produced.

## Where each section stands

| § | Asked for | Status | Where |
|---|---|---|---|
| 1 | Understand the existing bot | **done** | `CLAUDE.md` § Architecture, "Two parallel implementations" |
| 2 | Establish a baseline | **done** | below; `data/reports/XAUUSDm_20260906_152553_*`, `BTCUSDm_20260906_153640_*` |
| 3 | Identify weaknesses | **done** | below; `backend/scripts/diagnose.py` |
| 4 | Improve signal quality | **not done, deliberately** | no confirmation can rescue a signal that carries no information — see § 3 below |
| 5 | Improve entry logic | **not done, deliberately** | same reason |
| 6 | Optimize SL/TP | **done — measured, nothing shipped** | `backend/scripts/sweep.py`; 160 cells, none positive |
| 7 | Dynamic position sizing | **done in research; partially live** | `backend/backtest/risk.py`; live `risk_pct`, ships off. **The caps are not wired live** |
| 8 | Market regime detection | **measured, not acted on** | event study by 2-year block; `by_group` in every report |
| 9 | Session optimization | **measured, not acted on** | session / day-of-week breakdowns in every `run_baseline` report |
| 10 | Long vs short | **done** | below — Bitcoin's asymmetry is mostly financing, not direction |
| 11 | Avoid overfitting | **done, and it is the main finding** | `sweep.py` train/holdout split |
| 12 | Realistic trading costs | **done** | spread, commission, entry/exit slippage, **stop slippage**, **swap** |
| 13 | Optimize for the right objective | **done** | sweep objective is a t-statistic on `expectancy_r`, ranked on the neighbourhood |
| 14 | Trade filtering | **not done** | one candidate identified and untested — see "Remaining weaknesses" |
| 15 | Logging & explainability | **done** | ledger CSV per run; `trades` table folded from MT5 deals |
| 16 | Compare every change | **done** | `sweep.py` (grid) + `diagnose.py` (ledger) |
| 17 | Preserve safety | **done** | no martingale, no averaging down, no stop removal; see the entry-bar bug below |
| 18 | Code quality | **done** | strategy / execution / risk / indicators separated; 275 python + 25 frontend tests |
| 19 | Final optimization report | **this document** | |

---

## § 2 — Baseline

Shipped configuration (70-pip stop, 100-pip target, 50-pip scale-out trigger,
`partial_fraction = 0.5`, centre-line exit **off**), M5, central cost scenario, fixed
0.1 lots on a $1,000 opening balance, full cached span, on the engine that resolves the
entry bar.

| | XAUUSDm | BTCUSDm |
|---|---:|---:|
| Total trades | 2,807 | 1,279 |
| Winning / losing | 1,460 / 1,347 | 731 / 548 |
| Win rate | 52.0% | 57.2% |
| Net P&L | **−$24,181** | **−$3,429** |
| Profit factor | 0.76 | 0.91 |
| Average win / loss | $51.40 / $73.66 | $49.05 / $71.69 |
| Expectancy per trade | **−0.12 R** | **−0.04 R** |
| Max drawdown | 2313% | 326% |
| Report | `XAUUSDm_20260906_152553_*` | `BTCUSDm_20260906_153640_*` |

Every run also stores three cost scenarios (optimistic / central / stress), the full trade
ledger with MAE, MFE, bars held, exit reason and the band width at entry, and breakdowns
by session, day of week, direction and exit reason — §§ 9, 10 and 15 of the brief.

**Read the expectancy, not the drawdown.** A fixed 0.1 lots on $1,000 drives the balance
below zero and the engine has no ruin model, so a "2313%" drawdown is arithmetic rather
than a risk measure. With the equity floor from § 7 switched on and a $100,000 account,
gold's drawdown is a readable **24.9%** — the same edge, finally expressed in a number
that means something.

**Two figures worth holding on to when reading any older result:**

- The **legacy** close-only, cost-free engine — the one still behind the dashboard's
  Backtest page — reports **+$16,960 and a 1.23 profit factor** on the same gold data and
  the same period the honest engine scores at −$24,181 and 0.76. That is the size of the
  gap between a plausible-looking backtest and an executable one, on one instrument, with
  no parameter changed. Do not decide anything from that page.
- Bitcoin's swap is real money: `swap_long` is **−1638.6 points a day**, tripled on
  Friday, against `swap_short = 0`. Longs account for **$2,261** of Bitcoin's $3,429 loss
  and shorts for $1,168, so § 10's long-vs-short comparison is mostly a *financing* result
  on this instrument, not a directional one. No report written before 2026-09-06 charged
  it at all.

---

## § 3 — Weaknesses, and one that was ours rather than the market's

### The measurement was wrong first

Before any conclusion about the strategy could be trusted, the engine had to be. It had a
defect for its entire existence: the intrabar SL/TP check was guarded by
`i > entry_index`, so **the bar a trade filled on was never tested against its own stop or
target**. The first check landed on the *next* bar's open, which the gap rule then booked
as a gap fill at whatever price the skipped bar had reached.

On gold M5 that touched **47% of all trades**: 717 of 1,044 stop-outs filled an average of
**3.0 past a 7.00 stop**, and 365 of 622 take-profits filled **2.1 better than a 10.00
target** — which a limit order cannot do. The two errors did not cancel; the stop side was
2.8x the target side.

This is the § 17 case ("do not manipulate backtest results") arriving by accident rather
than by intent, and it is why every report produced before 2026-09-06 is archived under
`data/reports/pre-entry-bar-fix/` and none of its numbers should be quoted. **The live bot
was never affected** — it puts `sl` and `tp` inside the entry order, so the broker has held
both from the moment of fill all along.

`diagnose.py` now leads with a bars-held histogram precisely because a spike at 0 or 1 bars
is this bug's fingerprint.

### Why the strategy loses, which is not a tuning problem

Three measurements agree, and none of them involves a stop, a target or a cost model.

**1. Costs are the whole story on Bitcoin and none of it on gold.** Re-run with the cost
model switched off entirely:

| gross of all costs | XAUUSDm | BTCUSDm |
|---|---:|---:|
| Net P&L | −$13,133 | **+$221** |
| Profit factor | 0.86 | **1.01** |
| Expectancy | −0.07 R | **0.00 R** |

Bitcoin's signal is a coin flip that costs turn into a loss. Gold's signal loses money
before a single cost is charged, so no amount of cost engineering can reach it.

**2. Gold does not mean-revert at these horizons.** Variance ratio on log returns (below 1
is mean-reverting, 1.0 is a random walk):

| | q=2 | q=5 | q=10 | q=20 | q=50 |
|---|---:|---:|---:|---:|---:|
| XAUUSDm M5 | 1.023 | 1.008 | 1.051 | 0.998 | 0.974 |
| XAUUSDm H1 | 1.000 | 0.994 | 1.012 | 1.058 | 1.015 |
| BTCUSDm M5 | 1.010 | 0.967 | 0.954 | 0.936 | 0.892 |
| BTCUSDm H1 | 0.969 | 0.962 | 0.902 | 0.932 | 0.924 |

Gold is a random walk to three decimal places at every horizon this strategy trades.
**A mean-reversion strategy on gold is fading something that is not there.** Bitcoin does
revert (0.89–0.94) — and the effect is smaller than one round trip's spread, which is
exactly what the gross-of-costs table shows.

**3. The band touch adds nothing beyond the drift, and what it adds flips sign.** Event
study with the execution rules stripped away entirely — enter at the next bar's open, exit
*h* bars later, measured in ATR14, against the unconditional forward move as the baseline.
Drift-adjusted 20-bar edge, per 2-year block:

| | blocks | positive | mean edge | sign stable? |
|---|---:|---:|---:|---|
| XAUUSDm H1, fade the lower band | 10 | 3 | −0.372 ATR | **no** |
| XAUUSDm H1, fade the upper band | 10 | 5 | −0.166 ATR | **no** |
| BTCUSDm H1, fade the lower band | 9 | 2 | −0.427 ATR | **no** |
| BTCUSDm H1, fade the upper band | 9 | 2 | −0.467 ATR | **no** |

The one real-looking effect is gold M5 in 2025–26, where the lower band carries +0.114 ATR
at 20 bars (t = 3.8) and the upper band is strongly *anti*-predictive (−0.415 ATR,
t = −7.0). That is a window in which gold rose 43%: it is the bull run, not reversion.
Gold H1's own 2025 and 2026 blocks put the same long-side edge at −0.376 and −0.872.
**Do not act on the "long-only gold" reading** — it is the shape § 11 exists to stop.

### A structural weakness worth recording separately

The two instruments are given identical pip **counts** by decision, and that does not make
them the same strategy:

| | XAUUSDm | BTCUSDm |
|---|---:|---:|
| ATR14 (M5) | 5.04 | 142.3 |
| The 70-pip stop, in ATR14 | **1.39x** | **4.92x** |
| The 70-pip stop, in band half-widths | 0.51 | 1.80 |

Gold risks 1.4 bars of range and Bitcoin nearly 5. That single mismatch accounts for gold's
−0.92 average MAE against Bitcoin's −0.47, and for gold's 52% win rate against Bitcoin's
57%. The free knob that would bring them into line is the **timeframe**, not the geometry:
70 pips is 1.39x ATR on gold M5 and 1.33x on Bitcoin H1.

---

## § 19 — Final report

### Strategy changes

**None.** No entry rule, exit rule, filter or parameter was changed to improve performance,
because nothing tested improved it out of sample. Per critical rule 10 that is the correct
outcome, not an unfinished one.

What changed is the ability to tell:

| Change | Effect |
|---|---|
| Entry bar resolved against its own SL/TP | Removed a bias touching 47% of gold's trades |
| Stop slippage charged (`--slippage-stop`, one typical spread) | The pessimism the bug supplied by accident is now a number somebody chose |
| Broker swap charged from the spec sidecar | Bitcoin's financing is no longer free |
| Server-clock offset refuses an implausible reading | A weekend snapshot no longer stamps a whole capture 41 hours out |
| `backend/backtest/risk.py` | § 7 sizing + daily / streak / equity caps. **All off by default; off is a byte-for-byte no-op** |
| Live `risk_pct` | § 7 sizing on the live path. Ships **off** |
| Scale-out guard reads the position's entry deal | Equity-derived sizing can no longer make a reduced position look untouched |
| `backend/scripts/sweep.py` | § 11 and § 16, which were unimplementable without a held-out window |
| `backend/scripts/diagnose.py` | § 3 and § 15 at the ledger level |

### Performance comparison

The table § 19 asks for, filled in honestly. **This is a measurement change, not a strategy
improvement** — the "after" column is the same strategy measured correctly, and it is
worse, because the bug was flattering the per-trade figures while throttling re-entry.
Gold, central costs.

| Metric | Before the fix | After | Difference |
|---|---:|---:|---:|
| Win rate | 53.5% | 52.0% | −1.5 pp |
| Net P&L | −$16,819 | −$24,181 | −$7,362 |
| Profit factor | 0.825 | 0.756 | −0.069 |
| Expectancy | −0.104 R | −0.123 R | −0.019 R |
| Max drawdown | 1600% | 2313% | worse |
| Trades | 2,303 | 2,807 | +504 (+22%) |
| Average loss | $89.73 | **$73.66** | **−$16.07 — now the actual stop** |
| Largest win | $835.81 | **$103.33** | **−$732 — now reachable by a 100-pip target** |

The last two rows are the point: per-trade honesty was restored. The bug had been inflating
individual outcomes in both directions *and* suppressing re-entry, so removing it produces
22% more trades and a worse total. About $4,300 of the P&L difference is the newly-charged
stop slippage and swap rather than the fix itself — at matched costs the re-run is −$19,871.

Bitcoin is unchanged in structure and moved for the same reasons (its swap is new).

### Robustness

This is the section that decides the recommendation.

**In-sample vs out-of-sample.** `sweep.py` splits every run into a training window and a
held-out window and reports both.

- **160 cells on a common 11-month window** (M5/M15/M30/H1 x sl 70–150 x tp 100–220, both
  symbols): **not one cell has positive expectancy.** The best anywhere is BTCUSDm M30
  150/140 at −0.038 R; gold's best is M15 150/220 at −0.097 R. The shipped gold
  configuration (M5 70/100) lands at −0.142 R, among the worst gold cells in the grid.
- **Gold H4 over twelve years (2014–2026)** — the result to remember. Trained on the first
  70%, the best cell is sl 260 / tp 160 at **−0.025 R**, profit factor 0.859: close enough
  to break-even to be tempting. On the held-out final 30% the *same cell* is **−0.188 R**,
  profit factor **0.478**, drawdown **138% → 548%**.

  That is a seven-fold degradation, and it is the direct answer to "which parameters should
  we ship": on this data a configuration selected in-sample carries **no** information
  about the next period.

**Parameter sensitivity.** Two directions are stable enough to state, and both follow from
the ATR mismatch above: a wider stop is monotonically better on gold (70 → 150 pips), and a
fixed 70-pip stop degrades badly as the timeframe rises (M5 −0.142 R, M15 −0.208, M30
−0.359, H1 −0.467 at a 28% win rate). Neither turns any cell positive.

**Best and worst periods.** Gold M5 2025–26 is the only window where the signal looks
predictive, and § 3 shows that is the bull run rather than reversion. Every 2-year block
outside it flips sign.

**Sizing.** Tested across the full risk range on a $100,000 account with the equity floor
on:

| | trades | net P&L | expectancy | max DD | halted |
|---|---:|---:|---:|---:|---|
| fixed 0.1 lots | 2,807 | −$24,181 | −0.12 R | 24.9% | no |
| 0.25% of equity | 2,807 | −$57,361 | −0.12 R | 58.3% | no |
| 0.5% of equity | 2,807 | −$82,203 | −0.12 R | 83.0% | no |
| 1.0% of equity | 1,853 | −$90,029 | −0.12 R | 90.1% | **yes** |
| 2.0% of equity | 992 | −$90,072 | −0.11 R | 90.3% | **yes** |

`expectancy_r` is **flat at −0.12 across the entire column**. That is § 7's answer: sizing
scales the outcome and does not touch the edge, and with negative expectancy the
drawdown-minimising risk fraction is zero.

### Remaining weaknesses

1. **There is no edge to risk-manage.** Everything below is secondary to this.
2. **Risk-percent sizing barely functions on a small account.** Gold risks $700 per 1.0 lot,
   so the broker's smallest position already risks $7 — 0.23% of a $3,000 account. Below
   that floor every entry is skipped rather than rounded up, so on a $1,000 account 0.25%
   and 0.5% take **zero** trades out of 4,456 signals. On accounts this size the lot size
   really is the only risk control there is, and the contract size is what makes that true.
3. **§ 7's caps are research-only.** The daily loss cap, the consecutive-loss cooldown, the
   equity floor and the maximum-exposure check exist in `backend/backtest/risk.py` and are
   **not wired into the live loop**. `--max-daily-loss-pct` is not a live control. There is
   still no margin check.
4. **§ 14 is untouched.** One filter is worth testing before any other: `r_price /
   band_at_entry` — the stop expressed in units of the band the signal came from. Both
   columns are already in every ledger, and it sorts results monotonically on both symbols
   in opposite market trends, which makes it the most robust single quantity in this data.
   It has not been tested as a filter.
5. **A slow signal cannot be collected by this geometry.** Gold M5's long-side effect, if it
   is real at all, lives at h = 20–50 bars, while the average hold is ~2 bars against a
   1.4-ATR stop. Any attempt to harvest it must widen the stop *and* lengthen the hold
   together; doing one without the other just changes which end the loss comes from.
6. **The dashboard's Backtest page still runs the legacy engine** and reports a profit where
   the honest engine reports a large loss. It is kept for continuity, and it is a trap for
   anyone who does not read this far.
7. **The scale-out is enabled without evidence.** It was requested. It clips winners and
   does nothing for trades that run straight to the stop, and the sweep that used to be
   quoted in its defence was produced on the broken engine and has been withdrawn.

### Recommended production configuration

**Do not fund this strategy on either configured instrument.** That is the recommendation
the data supports, and critical rules 1 and 10 require stating it plainly rather than
shipping a tuned configuration that looks better in-sample.

Concretely:

- **Do not select parameters from the in-sample column of any sweep.** The H4 result
  measures what that costs: seven-fold degradation out of sample.
- **If it is run at all, run it on a demo account**, at the shipped configuration, which is
  at least the one every stored report describes: M5, 70/100/50 pips, scale-out on,
  centre-line exit off, `lot_size` 0.1, `risk_pct` 0.
- **Leave `risk_pct` at 0** on a small account. It is quantised away below ~0.25%, and it
  cannot improve a negative edge regardless.
- **Of the two, Bitcoin is the less bad** — gross expectancy 0.00 R against gold's −0.07 R,
  because Bitcoin genuinely does mean-revert and gold does not. That is not a recommendation
  to trade Bitcoin; it is where to look if the premise is revisited.
- **If the strategy is revisited, the order of work is:** re-derive the stop from ATR or from
  the band width rather than from a fixed pip count (weakness 5 and the ATR mismatch), test
  `r_price / band_at_entry` as an entry filter (weakness 4), and re-check the premise on an
  instrument whose variance ratio is actually below 1. Fixing the geometry on an instrument
  that does not revert cannot help.

### What was deliberately not built

Sections 4 and 5 asked for stronger confirmation logic and alternative entry approaches.
Neither was implemented, and the reason is § 3's event study rather than a shortage of time:
the band touch carries no information that survives a change of period, and a confirmation
filter applied to a signal with no edge removes trades without improving expectancy — it
only reduces the sample size that would reveal the problem. Adding indicators here would
have produced a better-looking backtest and a worse-understood strategy, which is what
§§ 4, 11 and 14 all warn against.
