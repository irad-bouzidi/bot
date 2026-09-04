"""BTCUSDm's geometry, and combining several symbols onto one account.

Two things are pinned here, and both are things that would fail quietly.

`SYMBOL_CONFIG` is expressed in pip COUNTS times a per-symbol pip size, so the
same 70/100 numbers mean 7.00/10.00 on gold and 700/1000 on Bitcoin. Nothing in
the live path would raise if a symbol picked up the wrong pip -- it would just
send a $0.70 stop on an $81,000 instrument and report a healthy bot. So the
worked example the symbol was added from (long 80500 -> TP 81500, SL 79800,
scale-out at 81000) is asserted directly against the config.

`combine_legacy_results` merges symbols onto ONE account. Its whole reason to
exist is that the combined drawdown is not recoverable from finished per-symbol
summaries, so the test that matters is the one where the merged trough is deeper
than either symbol's own.
"""

import numpy as np
import pandas as pd
import pytest

from backend.core.symbols import SYMBOL_CONFIG, SUPPORTED_SYMBOLS, price_levels


# ---------------------------------------------------------------------------
# the symbol table -- no MetaTrader5, no database
# ---------------------------------------------------------------------------

def test_btc_reproduces_the_worked_example():
    """Long at 80500: target 81500, stop 79800, scale-out + break-even at 81000."""
    cfg = SYMBOL_CONFIG["BTCUSDm"]
    entry = 80500.0
    pip = cfg["pip"]

    assert entry + cfg["tp_pips"] * pip == 81500.0
    assert entry - cfg["sl_pips"] * pip == 79800.0
    assert entry + cfg["be_trigger_pips"] * pip == 81000.0
    # Half the position out at the trigger, half left running to the target.
    assert cfg["partial_fraction"] == 0.5


def test_the_short_side_mirrors_it():
    """The rule is a distance, so a sell at 80500 is the long reflected."""
    cfg = SYMBOL_CONFIG["BTCUSDm"]
    entry, pip = 80500.0, cfg["pip"]
    assert entry - cfg["tp_pips"] * pip == 79500.0
    assert entry + cfg["sl_pips"] * pip == 81200.0
    assert entry - cfg["be_trigger_pips"] * pip == 80000.0


@pytest.mark.parametrize("symbol", SUPPORTED_SYMBOLS)
def test_the_scale_out_trigger_is_half_the_target(symbol):
    """One rule, both symbols: the trigger is half way to the take profit.

    It is the same statement as NWConfig.be_trigger_mode="tp_fraction" at 0.5, so
    a symbol whose trigger drifted off half would make the live bot and the
    research strategy measure different rules under the same name.
    """
    levels = price_levels(symbol)
    assert levels["be_trigger_tp_fraction"] == pytest.approx(0.5)
    assert levels["be_trigger_price"] == pytest.approx(levels["tp_price"] / 2.0)


@pytest.mark.parametrize("symbol", SUPPORTED_SYMBOLS)
def test_the_stop_is_shorter_than_the_target(symbol):
    """R:R above 1 for every configured symbol. `Trading Bot.md` forbids adding a
    symbol whose geometry does not work, and 700/1000 is the same 1:1.43 as
    gold's 7/10."""
    levels = price_levels(symbol)
    assert 0 < levels["sl_price"] < levels["tp_price"]


def test_price_levels_does_not_reuse_golds_pip():
    """The failure this guards is silent: gold's 0.1 pip applied to Bitcoin turns
    a 700-point stop into $70 of price, which no exception would catch."""
    assert price_levels("XAUUSDm")["sl_price"] == pytest.approx(7.0)
    assert price_levels("BTCUSDm")["sl_price"] == pytest.approx(700.0)


def test_both_symbols_risk_the_same_at_the_shipped_size():
    """0.1 lots is ~$70 on both -- a coincidence of the two CONTRACT sizes.

    Asserted because the dashboard prints this figure as the only risk warning
    there is, and because it is the fact that makes gold and Bitcoin comparable
    at equal lots in a combined backtest. It does NOT generalise: gold is 100 oz
    per lot over a 7.00 stop, Bitcoin is 1 BTC over a 700.00 one, and a third
    symbol will land wherever its contract size puts it.
    """
    for symbol in ("XAUUSDm", "BTCUSDm"):
        risk = price_levels(symbol)["risk_per_lot"] * SYMBOL_CONFIG[symbol]["lot_size"]
        assert risk == pytest.approx(70.0)


def test_migrate_seeds_a_row_for_every_configured_symbol():
    """`migrate` reads SYMBOL_CONFIG so a new symbol gets a stored lot size.

    It used to `ast`-parse the dict out of bot_manager's source; a symbol the
    parse missed would have had no `symbol_settings` row, and a missing row falls
    back to the code default -- the silent restore the whole persistence layer
    exists to prevent.
    """
    from backend.db.migrate import _code_defaults

    defaults = _code_defaults()
    assert set(defaults) == set(SYMBOL_CONFIG)
    for symbol, (lot, fraction) in defaults.items():
        assert lot == SYMBOL_CONFIG[symbol]["lot_size"]
        assert fraction == SYMBOL_CONFIG[symbol]["partial_fraction"]


# ---------------------------------------------------------------------------
# combining symbols onto one account
# ---------------------------------------------------------------------------

mt5 = pytest.importorskip(
    "MetaTrader5", reason="bot_manager imports MetaTrader5")

from backend.bot_manager import (  # noqa: E402
    TradingBot, combine_legacy_results, simulate_legacy,
)
from backend.core.errors import ConfigRejected  # noqa: E402


def _result(trades, trades_opened=None, **extra):
    """A simulate_legacy-shaped result carrying just the closed trades."""
    out = {
        "trades_opened": trades_opened if trades_opened is not None else len(trades),
        "partials_fired": 0,
        "partial_pl": 0.0,
        "closed_trades": [
            {"closed_at": pd.Timestamp(t[0]), "pl": t[1], "scaled_out": False}
            for t in trades
        ],
    }
    out.update(extra)
    return out


def test_a_bot_refuses_an_unknown_symbol_instead_of_trading_golds_config():
    with pytest.raises(ConfigRejected):
        TradingBot("EURUSDm")


def test_combined_pl_adds_up_and_the_drawdown_does_not():
    """The point of merging: two symbols losing at the SAME time compound.

    Gold loses 100 then recovers; Bitcoin loses 100 in the gap between. Each on
    its own never gives back more than 10%; together they are 200 down from a
    1000 peak, which no arithmetic on the two summaries would produce.
    """
    gold = _result([("2026-01-01 10:00", -100.0), ("2026-01-01 14:00", +100.0)])
    btc = _result([("2026-01-01 12:00", -100.0), ("2026-01-01 16:00", +100.0)])

    combined = combine_legacy_results({"XAUUSDm": gold, "BTCUSDm": btc}, 1000.0)

    assert combined["total_pl"] == pytest.approx(0.0)
    assert combined["final_balance"] == pytest.approx(1000.0)
    assert combined["wins"] == 2 and combined["losses"] == 2
    # 1000 -> 900 -> 800: 20% down from the peak, not the 10% either symbol saw.
    assert combined["max_drawdown"] == pytest.approx(20.0)


def test_drawdowns_that_do_not_overlap_are_not_added_together():
    """The other direction, and the reason summing is just as wrong as maxing.

    Same two symbols, same two 10% dips, but gold has fully recovered before
    Bitcoin starts losing. The account is never more than 10% below its peak, so
    the combined figure is 10 -- not the 20 the previous test produced from the
    identical per-symbol summaries. The interleaving IS the number.
    """
    gold = _result([("2026-01-01 10:00", -100.0), ("2026-01-01 11:00", +100.0)])
    btc = _result([("2026-01-01 12:00", -100.0), ("2026-01-01 13:00", +100.0)])

    combined = combine_legacy_results({"XAUUSDm": gold, "BTCUSDm": btc}, 1000.0)

    assert combined["max_drawdown"] == pytest.approx(10.0)
    assert combined["total_pl"] == pytest.approx(0.0)


def test_symbols_come_back_in_the_order_they_were_run():
    a = _result([("2026-01-01 10:00", 1.0)])
    b = _result([("2026-01-01 11:00", 1.0)])
    assert combine_legacy_results({"BTCUSDm": b, "XAUUSDm": a}, 1000.0)["symbols"] \
        == ["BTCUSDm", "XAUUSDm"]


def test_trades_opened_counts_every_entry_including_one_still_open():
    """win_rate's denominator is trades OPENED, matching the single-symbol
    engine, so an unclosed trade is counted as taken and as neither won nor
    lost. Collapsing the two would make a combined win rate incomparable with
    the per-symbol ones printed beside it."""
    gold = _result([("2026-01-01 10:00", 5.0)], trades_opened=2)
    btc = _result([("2026-01-01 11:00", -5.0)], trades_opened=1)

    combined = combine_legacy_results({"XAUUSDm": gold, "BTCUSDm": btc}, 1000.0)

    assert combined["trades_opened"] == 3
    assert combined["wins"] + combined["losses"] == 2
    assert combined["win_rate"] == pytest.approx(100.0 / 3)


def test_untimed_trades_are_not_interleaved_on_a_made_up_order():
    """Synthetic frames have no `time` column. Ordering them positionally would
    invent an interleaving across symbols, so the run says so instead."""
    gold = _result([("2026-01-01 10:00", 1.0)])
    gold["closed_trades"][0]["closed_at"] = None

    combined = combine_legacy_results({"XAUUSDm": gold}, 1000.0)

    assert combined["trades_ordered"] is False
    assert "could not be interleaved" in combined["warning"]


def test_the_per_symbol_results_do_not_carry_the_trade_stream():
    """`closed_trades` is 1700 rows on a real window. It is an input to the
    merge, not part of the answer, and /backtest stores its result as JSON."""
    gold = _result([("2026-01-01 10:00", 1.0)])
    btc = _result([("2026-01-01 11:00", 1.0)])

    combined = combine_legacy_results({"XAUUSDm": gold, "BTCUSDm": btc}, 1000.0)

    assert "closed_trades" not in combined
    for per in combined["per_symbol"].values():
        assert "closed_trades" not in per


# ---------------------------------------------------------------------------
# the stream the merge is built on
# ---------------------------------------------------------------------------

def _btc_config(**over):
    cfg = dict(SYMBOL_CONFIG["BTCUSDm"])
    cfg.update(over)
    return cfg


def test_closed_trades_reconcile_with_the_symbols_own_total():
    """Every closed trade, scored once, summing to the reported P&L.

    If the stream double-counted the scale-out -- banking it here AND in the
    runner's row -- a combined run would show a different total from the
    per-symbol ones printed underneath it.
    """
    close = [80000.0, 79000.0, 79600.0, 79000.0, 79000.0, 79000.0]
    df = pd.DataFrame({"close": close,
                       "time": pd.date_range("2026-01-01", periods=len(close),
                                             freq="5min")})
    n = len(close)
    outs = np.full(n, 85000.0)      # never reached -> no mean-reversion exit
    uppers = np.full(n, 90000.0)
    lowers = np.array([70000.0, 79500.0, 70000.0, 70000.0, 70000.0, 70000.0])

    res = simulate_legacy(df, outs, uppers, lowers, _btc_config(), 1000.0)

    assert res["partials_fired"] == 1
    assert sum(t["pl"] for t in res["closed_trades"]) == pytest.approx(res["total_pl"])
    assert len(res["closed_trades"]) == res["wins"] + res["losses"]
    # The trigger is 500 above the 79000 entry and this engine fills it at its
    # level, so 0.05 lots x 500 x 1 BTC/lot is banked...
    assert res["partial_pl"] == pytest.approx(500.0 * 0.05 * 1)
    # ...and the runner scratches at entry, so the trade nets exactly that.
    assert res["closed_trades"][0]["pl"] == pytest.approx(res["partial_pl"])


def test_a_btc_trade_carries_its_close_time():
    close = [80000.0, 79000.0, 78300.0, 78300.0]
    df = pd.DataFrame({"close": close,
                       "time": pd.date_range("2026-03-01", periods=len(close),
                                             freq="5min")})
    n = len(close)
    outs = np.full(n, 85000.0)
    uppers = np.full(n, 90000.0)
    lowers = np.array([70000.0, 79500.0, 70000.0, 70000.0])

    res = simulate_legacy(df, outs, uppers, lowers,
                          _btc_config(partial_fraction=0.0), 1000.0)

    assert len(res["closed_trades"]) == 1
    assert res["closed_trades"][0]["closed_at"] == pd.Timestamp("2026-03-01 00:10")
    # Straight to the 700-point stop: 0.1 lots x 700 x 1 BTC/lot.
    assert res["total_pl"] == pytest.approx(-70.0)
