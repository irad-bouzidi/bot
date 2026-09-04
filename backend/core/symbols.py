"""Per-symbol trading configuration -- the single definition, shared by every path.

This module exists because the same numbers are needed in three places that must
NOT be able to import each other:

  * `backend/bot_manager.py` (live orders) needs MetaTrader5;
  * `backend/db/migrate.py` needs to seed a row per symbol with no terminal;
  * `backend/scripts/run_baseline.py` needs the same geometry in PRICE units and
    must keep running offline, with neither MT5 nor Postgres.

So it lives here, in `backend/core`, which every one of them may already import
and which imports nothing but the standard library. `migrate` used to `ast`-parse
`SYMBOL_CONFIG` back out of bot_manager's source to dodge the MetaTrader5 import;
it imports this instead, so a second symbol cannot be added and then silently
skipped by a parse that no longer matches.

UNITS. `sl_pips`/`tp_pips`/`be_trigger_pips` are counts of `pip`, and `pip` is a
price distance -- not the broker's `point`. The product is what goes on the
order:

    XAUUSDm   70 x 0.1  =    7.00 stop,  100 x 0.1  =   10.00 target
    BTCUSDm  700 x 1.0  =  700.00 stop, 1000 x 1.0  = 1000.00 target

`profit_mult` is the contract size -- account currency per 1.0 price unit per
1.0 lot -- so P&L is `price_diff * lot_size * profit_mult`. Gold is 100 oz per
lot; Bitcoin is 1 BTC per lot, both read off `symbol_info().trade_contract_size`
on the live terminal. It is why the per-trade dollar risk does NOT carry across
symbols by lot count alone: it is a coincidence, not a rule, that 0.1 lots risks
~$70 on both of these. (The research stack does not use `profit_mult` at all --
`SymbolSpec.pl()` derives P&L from the broker's real tick value. Do not
reintroduce it there.)
"""

# `pip`               price distance of one "pip" for this symbol
# `lot_size`          position size. EDITABLE at runtime -- see EDITABLE_KEYS
# `sl_pips`/`tp_pips` stop and target, in pips
# `profit_mult`       contract size: price_diff * lot_size * profit_mult = P&L
# `be_trigger_pips`   profit distance at which the scale-out arms. Half the
#                     target, matching NWConfig.be_trigger_mode="tp_fraction"
# `partial_fraction`  proportion of the position closed at that trigger. It is a
#                     FRACTION and never a lot count, so it tracks lot_size
#                     instead of silently becoming a different share of the
#                     position when the size changes. EDITABLE at runtime, but
#                     only ever via scale_out_fraction(): the UI speaks lots,
#                     this dict does not.
# `exit_at_mean`      close the position when price returns to the envelope's
#                     CENTRE line, instead of leaving it to the stop, the
#                     break-even stop or the target. EDITABLE at runtime.
#                     Shipped OFF: the centre line sits at roughly `mult * mae`
#                     from entry -- about 6.00 on gold -- which is *between* the
#                     5.00 scale-out trigger and the 10.00 target, so it
#                     intercepted the scaled-out runner on nearly every trade
#                     and made the target effectively unreachable. It is a
#                     boolean rather than a distance because there is nothing to
#                     tune: the level is wherever the envelope puts it.
SYMBOL_CONFIG = {
    "XAUUSDm": {
        "pip": 0.1,
        "lot_size": 0.1,            # risks ~$70/trade at the 70-pip stop
        "sl_pips": 70,
        "tp_pips": 100,
        "profit_mult": 100,         # 100 oz per lot
        "be_trigger_pips": 50,      # 5.00 in price -- half of the 100-pip target
        "partial_fraction": 0.5,    # 0.05 out at +5.00, 0.05 runs to the target
        "exit_at_mean": False,      # centre line ~6.00 out: inside the target
    },
    # Same shape as gold, one pip = $1. A long at 80500 therefore targets 81500,
    # stops at 79800, and banks half at 81000 with the stop pulled to 80500 --
    # the worked example this symbol was added from.
    #
    # Bitcoin is 1 BTC per lot, so 0.1 lots over the 700-point stop is ~$70,
    # which happens to match gold's exposure at the same nominal size. Do not
    # read that as a rule (see the module docstring): it comes from the contract
    # size, and a third symbol will not inherit it.
    "BTCUSDm": {
        "pip": 1.0,
        "lot_size": 0.1,            # risks ~$70/trade at the 700-point stop
        "sl_pips": 700,
        "tp_pips": 1000,
        "profit_mult": 1,           # 1 BTC per lot
        "be_trigger_pips": 500,     # 500.00 in price -- half of the target
        "partial_fraction": 0.5,    # 0.05 out at +500, 0.05 runs to the target
        "exit_at_mean": False,      # centre line ~600 out: inside the target
    },
}

SUPPORTED_SYMBOLS = list(SYMBOL_CONFIG.keys())

# The dashboard can edit exactly these three keys; every other key above is
# fixed in code. They are persisted, because with no equity-based sizing anywhere
# in this bot the lot size IS the risk control: someone who lowers it to 0.02 to
# cut their exposure must not have 0.1 -- and ~$70 a trade -- quietly restored by
# a restart. `exit_at_mean` is persisted for the mirror-image reason: someone who
# switched the centre-line exit off must not have it restored by a restart and
# find the runner being closed before the target again.
#
# Note what is still NOT reachable from here: a stop, a target, a pip or a
# symbol. Every editable key either sizes a position or removes an exit; none of
# them can move a level or introduce an instrument. That is the invariant
# `repository.load_settings()` and `_load_settings()` are both written to hold.
EDITABLE_KEYS = ("lot_size", "partial_fraction", "exit_at_mean")

# Which of the above are NOT floats. `_validated()` branches on this, and it
# matters more than it looks: bool("false") is True, so a boolean that went
# through the float/str path would read as off everywhere it is displayed while
# actually being on.
BOOL_KEYS = ("exit_at_mean",)


def is_supported(symbol):
    # type: (str) -> bool
    return symbol in SYMBOL_CONFIG


def price_levels(symbol):
    # type: (str) -> dict
    """The pip counts above, converted once, in PRICE units.

    One conversion in one place. Every caller that needs a distance rather than a
    pip count reads it here, so a symbol whose pip is not 0.1 cannot pick up
    gold's arithmetic by being passed through a helper that assumed it.
    """
    cfg = SYMBOL_CONFIG[symbol]
    pip = float(cfg["pip"])
    return {
        "pip": pip,
        "sl_price": cfg["sl_pips"] * pip,
        "tp_price": cfg["tp_pips"] * pip,
        "be_trigger_price": cfg.get("be_trigger_pips", 0) * pip,
        # The trigger as a share of the target, which is how the research
        # strategy expresses it (NWConfig.be_trigger_tp_fraction). Kept derived
        # rather than stored so the two can never drift apart.
        "be_trigger_tp_fraction": (
            float(cfg.get("be_trigger_pips", 0)) / float(cfg["tp_pips"])
            if cfg.get("tp_pips") else 0.0),
        "risk_per_lot": cfg["sl_pips"] * pip * cfg["profit_mult"],
    }
