"""Per-trade record. The primary research artifact.

The old backtest returned 8 aggregate numbers and no trade list, so no post-hoc
analysis was possible at all. Everything the plan's diagnosis phase needs -- the
exit-reason census, MAE/MFE surfaces, spread sensitivity, session and regime
breakdowns -- is a view over this table, and it also satisfies the
explainability requirement (every trade records why it was taken).
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from backend.core.types import Side

# Exit reasons. Kept as plain strings so they survive a CSV round trip.
EXIT_SL = "sl"
EXIT_TP = "tp"
EXIT_SIGNAL = "signal"          # strategy exit, e.g. price returned to the mean
EXIT_END_OF_DATA = "end_of_data"
EXIT_RISK = "risk_halt"
# The remainder of a scaled-out trade, stopped at its moved-to-entry stop. Kept
# distinct from EXIT_SL because folding the two together would hide the whole
# effect of the break-even rule: a scratch on the remainder and a full 1R loss
# are the same "sl" in the census, and the exit-reason table is how this
# strategy's behaviour is actually diagnosed.
EXIT_BE = "be_stop"


@dataclass
class TradeRecord:
    trade_id: int
    symbol: str
    side: str
    entry_time: datetime
    entry_index: int
    entry_price: float
    volume: float                       # volume OPENED; never reduced, so 1R and
                                        # commission stay anchored to the original risk
    sl_price: float
    tp_price: float
    r_price: float                      # stop distance in price = 1R
    entry_reason: str = ""
    signal_strength: float = 0.0

    # -- scale-out / break-even ------------------------------------------------
    # A trade can now close in two pieces. `volume` above is what was opened;
    # `remaining_volume` is what is still exposed after the partial.
    remaining_volume: float = 0.0        # set to `volume` at open
    be_trigger_price: float = 0.0        # absolute price that arms the rule; 0 = disabled
    partial_fraction: float = 0.0        # intended proportion to scale out
    partial_volume: float = 0.0          # lots closed at the trigger
    partial_price: float = 0.0           # fill price of the partial leg
    partial_pl: float = 0.0              # gross P&L banked on the partial leg
    partial_index: Optional[int] = None
    partial_time: Optional[datetime] = None
    be_moved: bool = False               # stop has been pulled to entry

    exit_time: Optional[datetime] = None
    exit_index: Optional[int] = None
    exit_price: float = 0.0
    exit_reason: str = ""

    gross_pl: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    net_pl: float = 0.0
    pnl_r: float = 0.0                  # net P&L in R units -- the comparable one

    mae_price: float = 0.0              # worst adverse excursion, price units
    mfe_price: float = 0.0              # best favourable excursion
    mae_r: float = 0.0
    mfe_r: float = 0.0

    bars_held: int = 0
    duration_s: float = 0.0
    spread_at_entry: float = 0.0
    atr_at_entry: float = 0.0
    band_at_entry: float = 0.0
    session: str = ""
    day_of_week: str = ""
    hour_utc: int = 0
    is_open: bool = False
    features: Dict[str, float] = field(default_factory=dict)

    def to_row(self):
        # type: () -> Dict[str, Any]
        d = asdict(self)
        d.pop("features", None)
        return d


def session_of(ts):
    # type: (datetime) -> str
    """UTC session buckets.

    Rollover (21-24 UTC) is its own bucket rather than being folded into New York:
    it is where gold spreads blow out, and merging it contaminates the NY numbers.
    """
    h = ts.hour
    if h < 7:
        return "asian"
    if h < 12:
        return "london"
    if h < 16:
        return "overlap"
    if h < 21:
        return "newyork"
    return "rollover"


def to_frame(trades):
    # type: (List[TradeRecord]) -> pd.DataFrame
    if not trades:
        return pd.DataFrame(columns=[f for f in TradeRecord.__dataclass_fields__
                                     if f != "features"])
    return pd.DataFrame([t.to_row() for t in trades])
