# Superseded: produced before the entry-bar fix

Every report in this directory was produced by an engine that did not check
SL/TP on the bar a trade filled on (`engine.py`, `i > open_trade.entry_index`).
The first stop check therefore landed on the NEXT bar's open, which rule 5 then
booked as a gap fill.

Measured on `XAUUSDm_20260906_143218_ledger.csv`, which is the newest of them:

* 717 of 1,044 stop-outs exited on the bar after entry, filling an average of
  **3.015 past a 7.00 stop** (trades held three bars or more overshoot ~0.10).
* 365 of 622 take-profits filled an average of **2.128 BETTER than the 10.00
  target**, which a limit order cannot do.
* 47% of all trades are affected.

They are kept because they are the evidence for the bias, not because their
numbers describe the strategy. Do not quote a P&L, drawdown, win rate,
expectancy or exit census from this directory. In particular the `exit_at_mean`
A/B (`*_172844` / `*_172858` and the BTC pair) compared two runs that were both
distorted, and its conclusion does not carry.

The live bot was never affected: `bot_manager.open_trade` sends `sl` and `tp`
inside the entry order, so the broker has held both from the fill all along.
