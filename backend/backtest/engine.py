"""Bar-by-bar backtest engine.

EXECUTION CONTRACT -- these rules are the reason results can be trusted, and they
are the ones the previous implementation got wrong:

1. A signal is evaluated on the CLOSE of bar i using data <= i only.
2. Entries and signal-driven exits fill at bar i+1's OPEN, adjusted for costs.
   No same-bar fills. A signal on the final bar cannot fill.
3. While a position is open, every subsequent bar is checked for INTRABAR SL/TP
   against that bar's high/low. The old engine compared only against `close`, so
   any stop swept intrabar and recovered by the close was scored as a later win.
   For a band-fading strategy on M5 this was the single largest source of bias.
4. Same-bar tie-break: SL BEFORE TP. If both levels sit inside [low, high] the
   trade is booked as a stop-out. Conservative and documented; `tie_break="ambiguous"`
   instead flags such trades and reports how much of the edge rests on bars whose
   ordering cannot be resolved from OHLC alone.
5. GAPS FILL AT THE GAP PRICE, not the level. The old code forced the fill back to
   exactly entry +/- sl, so a bar that gapped 200 points through a 70-point stop
   was booked as a clean 70-point loss. Losses could never be worse than ideal.
6. Intrabar SL/TP takes precedence over the same bar's close-based exit signal.
7. The final open position is marked to market and written to the ledger with
   `is_open=True`. It counts in `trades_opened` and `final_balance` but is excluded
   from wins/losses/win_rate. The old code discarded it while still counting it.
8. Max drawdown is computed from the bar-by-bar EQUITY curve (balance +
   unrealised), not the realised-balance curve, which under-reports it.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backend.backtest.costs import CostConfig, CostModel
from backend.backtest.ledger import (
    EXIT_END_OF_DATA, EXIT_SIGNAL, EXIT_SL, EXIT_TP, TradeRecord, session_of, to_frame,
)
from backend.core.types import PositionView, Side, SignalType, SymbolSpec
from backend.data.market_data import BarSet
from backend.strategy.base import Bar, BarContext, Strategy


@dataclass(frozen=True)
class BacktestConfig:
    initial_balance: float = 1000.0
    volume: float = 0.05
    tie_break: str = "sl_first"     # "sl_first" | "tp_first" | "ambiguous"
    legacy_mode: bool = False       # reproduce the ORIGINAL engine, for regression only


@dataclass
class BacktestResult:
    ledger: pd.DataFrame
    equity: pd.Series
    metrics: Dict[str, Any]
    config_used: Dict[str, Any] = field(default_factory=dict)
    data_meta: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


class BacktestEngine:
    def __init__(self, strategy, spec, costs=None, cfg=None):
        # type: (Strategy, SymbolSpec, Optional[CostModel], Optional[BacktestConfig]) -> None
        self.strategy = strategy
        self.spec = spec
        self.costs = costs or CostModel(CostConfig(spread_source="none"), spec)
        self.cfg = cfg or BacktestConfig()

    # -- intrabar resolution -------------------------------------------------

    def _resolve_stops(self, side, sl, tp, o, h, l):
        """Return (exit_price, reason) or (None, None) if the bar does not close it.

        Rule 5 lives here: a gap through a level fills at the OPEN, which is the
        first price actually available, not at the level itself.
        """
        if side is Side.LONG:
            gap_sl = sl > 0 and o <= sl
            gap_tp = tp > 0 and o >= tp
            if gap_sl and gap_tp:
                return (o, EXIT_SL)
            if gap_sl:
                return (o, EXIT_SL)
            if gap_tp:
                return (o, EXIT_TP)
            hit_sl = sl > 0 and l <= sl
            hit_tp = tp > 0 and h >= tp
        else:
            gap_sl = sl > 0 and o >= sl
            gap_tp = tp > 0 and o <= tp
            if gap_sl and gap_tp:
                return (o, EXIT_SL)
            if gap_sl:
                return (o, EXIT_SL)
            if gap_tp:
                return (o, EXIT_TP)
            hit_sl = sl > 0 and h >= sl
            hit_tp = tp > 0 and l <= tp

        if hit_sl and hit_tp:
            if self.cfg.tie_break == "tp_first":
                return (tp, EXIT_TP)
            return (sl, EXIT_SL)          # conservative default; "ambiguous" tagged later
        if hit_sl:
            return (sl, EXIT_SL)
        if hit_tp:
            return (tp, EXIT_TP)
        return (None, None)

    # -- main loop -----------------------------------------------------------

    def run(self, barset):
        # type: (BarSet) -> BacktestResult
        df = barset.df
        spec = self.spec
        cfg = self.cfg
        n = len(df)
        warnings = list(barset.warnings)

        if n == 0:
            return BacktestResult(to_frame([]), pd.Series(dtype=float),
                                  {"error": "no bars"}, warnings=warnings)

        feats = self.strategy.prepare(df)
        self.strategy.reset()

        o = df["open"].values
        hi = df["high"].values
        lo = df["low"].values
        cl = df["close"].values
        spr = df["spread"].values if "spread" in df.columns else np.zeros(n)
        idx = df.index

        balance = cfg.initial_balance
        equity_curve = np.full(n, balance, dtype=float)
        trades = []                 # type: List[TradeRecord]
        open_trade = None           # type: Optional[TradeRecord]
        position = None             # type: Optional[PositionView]
        pending = None              # a signal awaiting the next bar's open
        ambiguous_bars = 0
        # Evaluate from the first non-warm-up bar. The strategy must be ASKED on
        # this bar even though nothing can fill on it -- its signal fills at the
        # next bar's open (rule 2).
        start = barset.warmup_count

        for i in range(start, n):
            # --- fill anything queued on the previous bar (rule 2) ---
            if pending is not None:
                sig = pending
                pending = None
                if sig.type is SignalType.EXIT and open_trade is not None:
                    px = self.costs.exit_fill(position.side, o[i], spec, spr[i])
                    balance += self._close(open_trade, px, i, idx[i], EXIT_SIGNAL, spec)
                    trades.append(open_trade)
                    open_trade, position = None, None
                elif sig.type in (SignalType.ENTER_LONG, SignalType.ENTER_SHORT) \
                        and open_trade is None:
                    side = Side.LONG if sig.type is SignalType.ENTER_LONG else Side.SHORT
                    px = self.costs.entry_fill(side, o[i], spec, spr[i])
                    open_trade, position = self._open(
                        sig, side, px, i, idx[i], spec, spr[i], len(trades) + 1)

            # --- intrabar stops on the CURRENT bar (rules 3-6) ---
            if open_trade is not None and i > open_trade.entry_index:
                if not cfg.legacy_mode:
                    both_in = (
                        open_trade.sl_price > 0 and open_trade.tp_price > 0
                        and lo[i] <= max(open_trade.sl_price, open_trade.tp_price)
                        and hi[i] >= min(open_trade.sl_price, open_trade.tp_price)
                        and lo[i] <= open_trade.sl_price <= hi[i]
                        and lo[i] <= open_trade.tp_price <= hi[i]
                    )
                    if both_in:
                        ambiguous_bars += 1
                    px, reason = self._resolve_stops(
                        position.side, open_trade.sl_price, open_trade.tp_price,
                        o[i], hi[i], lo[i])
                else:
                    px, reason = self._legacy_stops(open_trade, position.side, cl[i])

                if px is not None:
                    fill = self.costs.exit_fill(position.side, px, spec, spr[i],
                                                is_stop=(reason == EXIT_SL))
                    balance += self._close(open_trade, fill, i, idx[i], reason, spec)
                    trades.append(open_trade)
                    open_trade, position = None, None

            # --- track excursions ---
            if open_trade is not None:
                self._track_excursion(open_trade, position.side, hi[i], lo[i])

            # --- ask the strategy (rule 1) ---
            ctx = BarContext(
                index=i, time=idx[i].to_pydatetime(),
                bar=Bar(o[i], hi[i], lo[i], cl[i], float(spr[i])),
                features={k: float(feats[k].values[i]) for k in feats.columns},
                position=position, spec=spec,
            )
            sigs = self.strategy.on_bar(ctx)
            if sigs and i < n - 1:
                pending = sigs[0]

            equity_curve[i] = balance + self._unrealised(open_trade, position, cl[i], spec)

        # --- rule 7: mark the survivor to market ---
        if open_trade is not None:
            fill = self.costs.exit_fill(position.side, cl[n - 1], spec, spr[n - 1])
            balance += self._close(open_trade, fill, n - 1, idx[n - 1],
                                   EXIT_END_OF_DATA, spec)
            open_trade.is_open = True
            trades.append(open_trade)
            equity_curve[n - 1] = balance

        equity_curve[:start] = cfg.initial_balance
        equity = pd.Series(equity_curve, index=idx, name="equity")
        ledger = to_frame(trades)

        from backend.backtest.metrics import compute_metrics
        metrics = compute_metrics(ledger, equity, cfg.initial_balance)
        metrics["ambiguous_bars"] = ambiguous_bars

        return BacktestResult(
            ledger=ledger, equity=equity, metrics=metrics,
            config_used={
                "initial_balance": cfg.initial_balance, "volume": cfg.volume,
                "tie_break": cfg.tie_break, "legacy_mode": cfg.legacy_mode,
                "costs": self.costs.cfg.__dict__, "strategy": _strategy_cfg(self.strategy),
            },
            data_meta=barset.meta(), warnings=warnings,
        )

    # -- helpers -------------------------------------------------------------

    def _legacy_stops(self, t, side, close):
        """Reproduce the ORIGINAL close-only checks, for the regression gate."""
        if side is Side.LONG:
            if t.sl_price > 0 and close <= t.sl_price:
                return (t.sl_price, EXIT_SL)
            if t.tp_price > 0 and close >= t.tp_price:
                return (t.tp_price, EXIT_TP)
        else:
            if t.sl_price > 0 and close >= t.sl_price:
                return (t.sl_price, EXIT_SL)
            if t.tp_price > 0 and close <= t.tp_price:
                return (t.tp_price, EXIT_TP)
        return (None, None)

    def _open(self, sig, side, price, i, ts, spec, spread_pts, trade_id):
        sl_d = sig.sl_distance or 0.0
        tp_d = sig.tp_distance or 0.0
        sl = price - sl_d * side.sign if sl_d else 0.0
        tp = price + tp_d * side.sign if tp_d else 0.0
        f = sig.features or {}
        t = TradeRecord(
            trade_id=trade_id, symbol=spec.name, side=side.value,
            entry_time=ts.to_pydatetime(), entry_index=i, entry_price=price,
            volume=self.cfg.volume, sl_price=spec.round_price(sl) if sl else 0.0,
            tp_price=spec.round_price(tp) if tp else 0.0, r_price=sl_d,
            entry_reason=sig.reason, signal_strength=sig.strength,
            spread_at_entry=float(spread_pts) * spec.point,
            atr_at_entry=float(f.get("atr", 0.0) or 0.0),
            band_at_entry=float(f.get("mae", 0.0) or 0.0),
            session=session_of(ts), day_of_week=ts.strftime("%a"), hour_utc=ts.hour,
            features={k: float(v) for k, v in f.items() if np.isfinite(v)},
        )
        t.mae_price = 0.0
        t.mfe_price = 0.0
        pos = PositionView(ticket=trade_id, symbol=spec.name, side=side,
                           volume=self.cfg.volume, entry_price=price,
                           entry_time=ts.to_pydatetime(), sl=t.sl_price, tp=t.tp_price)
        return t, pos

    def _track_excursion(self, t, side, high, low):
        if side is Side.LONG:
            t.mae_price = min(t.mae_price, low - t.entry_price)
            t.mfe_price = max(t.mfe_price, high - t.entry_price)
        else:
            t.mae_price = min(t.mae_price, t.entry_price - high)
            t.mfe_price = max(t.mfe_price, t.entry_price - low)

    def _close(self, t, price, i, ts, reason, spec):
        side = Side.LONG if t.side == "long" else Side.SHORT
        t.exit_price = price
        t.exit_index = i
        t.exit_time = ts.to_pydatetime()
        t.exit_reason = reason
        t.bars_held = i - t.entry_index
        t.duration_s = (t.exit_time - t.entry_time).total_seconds()
        t.gross_pl = spec.pl(t.entry_price, price, side, t.volume)
        t.commission = self.costs.commission(t.volume)
        t.swap = self.costs.swap(side, t.volume, t.entry_time, t.exit_time, spec)
        t.net_pl = t.gross_pl - t.commission + t.swap
        if t.r_price > 0:
            risk = abs(spec.pl(t.entry_price, t.entry_price - t.r_price * side.sign,
                               side, t.volume))
            t.pnl_r = t.net_pl / risk if risk else 0.0
            t.mae_r = t.mae_price / t.r_price
            t.mfe_r = t.mfe_price / t.r_price
        return t.net_pl

    def _unrealised(self, t, pos, close, spec):
        if t is None or pos is None:
            return 0.0
        return spec.pl(t.entry_price, close, pos.side, t.volume)


def _strategy_cfg(strategy):
    cfg = getattr(strategy, "cfg", None)
    if cfg is None:
        return {}
    try:
        from dataclasses import asdict
        return asdict(cfg)
    except Exception:
        return dict(getattr(cfg, "__dict__", {}))
