"""Position sizing and the caps that bound a losing run.

`Trading Bot.md` section 7 asks for risk-based sizing and for a maximum daily
loss, a maximum consecutive-loss protection and a maximum open exposure. None of
it existed anywhere: the engine traded a constant `BacktestConfig.volume` and had
no floor at all, which is why a stored gold report reads `max_drawdown: 2313%`
against `final_balance: -$24,181` on a $1,000 account. A percentage of a peak
that the balance has since gone *below zero* from is not a drawdown; it is
arithmetic. This module is what makes that number mean something.

It lives in `backend/backtest/` and NOT in the empty `backend/risk/` package,
which is reserved for pulling order-sending out of `bot_manager`. The research
stack may not import the database or MetaTrader5, and
`tests/test_db_invariants.py` parametrises that guard over `backtest` -- so
keeping it here keeps it guarded.

**Everything defaults to off.** `RiskConfig()` reproduces the fixed-volume engine
exactly, and `test_risk_config_defaults_reproduce_the_fixed_volume_engine` pins
it. A non-zero default would silently re-size every stored configuration and
every test the moment this file landed, which is the same class of silent change
`init_persistence` refuses to boot for.
"""

from dataclasses import dataclass

# Real broker clocks sit between UTC-12 and UTC+14; a "trading day" is therefore
# taken in the BROKER's day, not the local one, or a daily loss cap resets in the
# middle of the London session.
SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class RiskConfig:
    # PERCENT of equity risked at the stop. 1.0 means 1%. 0.0 disables sizing
    # entirely and `BacktestConfig.volume` is used, which is what every stored
    # report and every existing test did.
    risk_pct_per_trade: float = 0.0

    # Percent of the day's OPENING equity. Blocks new entries for the rest of
    # the broker day; it deliberately does NOT close a running position, because
    # closing one realises the very loss the cap exists to bound.
    max_daily_loss_pct: float = 0.0

    # After this many losses in a row, refuse entries for `cooldown_bars`.
    max_consecutive_losses: int = 0
    cooldown_bars: int = 0

    # Absolute floor in account currency. Breaching it is TERMINAL: the run
    # stops. Without it the engine happily trades a negative balance, and every
    # drawdown percentage past that point is meaningless.
    min_equity: float = 0.0

    # The engine holds `open_trade` as a scalar, so it is structurally
    # single-position. Anything else must be REFUSED rather than quietly
    # downgraded -- a config the engine cannot honour is a config whose results
    # would not describe what was asked for.
    max_open_positions: int = 1

    # What to do when the risk-sized volume rounds below the broker's minimum.
    # "skip" is the default because `SymbolSpec.round_volume` CLAMPS UP to
    # volume_min: on a $1,000 account at 0.5%, gold's minimum 0.01 lot risks $7,
    # which is 0.7% -- so "clamp" silently risks more than was asked for, and
    # does it precisely when the account is smallest.
    min_volume_policy: str = "skip"      # "skip" | "clamp"

    def __post_init__(self):
        if self.max_open_positions != 1:
            raise ValueError(
                "max_open_positions=%r: this engine holds one position at a "
                "time by construction. Refusing rather than silently trading a "
                "different configuration from the one requested."
                % (self.max_open_positions,))
        if self.min_volume_policy not in ("skip", "clamp"):
            raise ValueError("min_volume_policy must be 'skip' or 'clamp'")
        for name in ("risk_pct_per_trade", "max_daily_loss_pct", "min_equity",
                     "cooldown_bars", "max_consecutive_losses"):
            if getattr(self, name) < 0:
                raise ValueError("%s cannot be negative" % name)

    @property
    def enabled(self):
        # type: () -> bool
        return bool(self.risk_pct_per_trade or self.max_daily_loss_pct
                    or self.max_consecutive_losses or self.min_equity)


def size_for_risk(cfg, spec, equity, entry_price, sl_price, side_sign):
    """Lots that put `risk_pct_per_trade` of `equity` at the stop, or None.

    Returns None when the trade should be SKIPPED -- either the stop distance is
    unusable, or the honest size rounds below what the broker will accept and
    `min_volume_policy` is "skip".

    The money at risk is derived through `SymbolSpec.pl()`, the single P&L
    implementation, rather than by multiplying a contract size here. That is
    what stops this function quietly assuming gold's arithmetic on an instrument
    whose tick value is different.
    """
    if cfg.risk_pct_per_trade <= 0:
        return None                      # caller falls back to the fixed volume
    if equity <= 0 or entry_price <= 0:
        return None
    risk_cash = equity * cfg.risk_pct_per_trade / 100.0
    if risk_cash <= 0:
        return None

    # Money lost per 1.0 lot if this trade goes to its stop.
    from backend.core.types import Side
    side = Side.LONG if side_sign > 0 else Side.SHORT
    per_lot = abs(spec.pl(entry_price, sl_price, side, 1.0))
    if per_lot <= 0:
        return None

    raw = risk_cash / per_lot
    step = spec.volume_step or 0.01
    lots = int(raw / step) * step        # round DOWN: never risk more than asked
    lots = round(lots, 10)
    if lots < spec.volume_min:
        if cfg.min_volume_policy == "clamp":
            return float(spec.volume_min)
        return None
    return float(min(lots, spec.volume_max))
