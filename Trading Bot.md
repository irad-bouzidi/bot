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