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
9. SCALE-OUT: when a favourable move reaches `be_trigger_distance`, part of the
   position closes and the stop moves to entry. A bar that reaches both the
   trigger and the original stop is booked as a full stop-out with NO partial,
   because OHLC cannot order the two. The new break-even stop goes live on the
   NEXT bar; on the firing bar only a TP can still close the remainder, since
   entry -> trigger -> TP is the one ordering OHLC does determine. See
   `_resolve_bar` for why that asymmetry is the right way round. The remainder's
   break-even exit is reported as `be_stop`, never folded into `sl`.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backend.backtest.costs import CostConfig, CostModel
from backend.backtest.ledger import (
    EXIT_BE, EXIT_END_OF_DATA, EXIT_SIGNAL, EXIT_SL, EXIT_TP, TradeRecord,
    session_of, to_frame,
)
from backend.core.types import PositionView, Side, SignalType, SymbolSpec
from backend.data.market_data import BarSet
from backend.strategy.base import Bar, BarContext, Strategy


@dataclass(frozen=True)
class BacktestConfig:
    initial_balance: float = 1000.0
    volume: float = 0.1        # matches SYMBOL_CONFIG lot_size; keep the two equal
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

    # -- scale-out / break-even (rule 9) -------------------------------------

    def _trigger_touch(self, side, b, o, h, l):
        """First available price at which scale-out trigger `b` fills, or None.

        Same gap principle as rule 5: if the bar OPENS beyond the trigger, the
        first price actually available is the open. Here that is in the trade's
        favour, so it books at the open rather than at the trigger -- the mirror of
        a gapped stop booking worse than its level, not a free improvement.
        """
        if b <= 0:
            return None
        if side is Side.LONG:
            if o >= b:
                return o
            return b if h >= b else None
        if o <= b:
            return o
        return b if l <= b else None

    def _resolve_bar(self, t, side, o, h, l):
        """Everything that can happen to open trade `t` during one bar.

        Returns (partial_price | None, exit_price | None, exit_reason, ambiguous).

        Ordering, in the same conservative spirit as rules 4-5:

        * The trigger sits strictly between entry and TP, so any path that reached
          TP must have passed the trigger first. That ordering IS determinable from
          OHLC, so the partial is always booked before a TP.
        * A bar that reaches both the trigger (in profit) and the ORIGINAL stop (in
          loss) is not resolvable from OHLC. It is booked as a full stop-out with no
          partial -- the pessimistic reading, consistent with tie_break="sl_first".
          Booking the partial here instead would let every losing trade first bank a
          risk-free profit, which is exactly the flattery this engine exists to avoid.
        * A break-even stop created midway through a bar is NOT live on that bar at
          all -- it did not exist when the bar's low printed, and OHLC cannot order
          the low against the trigger touch. From the next bar on it is an ordinary
          stop and rule 5 applies to it normally.
        """
        sl, tp, be = t.sl_price, t.tp_price, t.be_trigger_price
        partial_px = None
        ambiguous = False

        if not t.be_moved and be > 0:
            b_px = self._trigger_touch(side, be, o, h, l)
            sl_px, sl_reason = self._resolve_stops(side, sl, 0.0, o, h, l)
            if b_px is not None and sl_px is not None:
                # Trigger and original stop both touched: order unknowable.
                return (None, sl_px, EXIT_SL, True)
            if sl_px is not None:
                return (None, sl_px, EXIT_SL, False)
            if b_px is not None:
                partial_px = b_px
                # The break-even stop is live from the NEXT bar, not this one.
                #
                # On this bar the stop is created part-way through, and OHLC cannot
                # order the bar's low against the moment the trigger was touched.
                # Both readings need exactly one visit to each extreme, so neither is
                # more physical than the other -- but they are not symmetric in how
                # wrong they can be. The common shape for a mean-reversion long is a
                # bar that OPENS below entry and then rallies through the trigger:
                # stopping that out at break-even books an exit against a stop that
                # did not exist when the low printed, and it happens on a large
                # fraction of exactly the trades the rule is supposed to help. The
                # opposite error costs one bar of exposure on the REMAINDER only,
                # with its stop already at entry -- bounded by a fraction of a bar's
                # range, not by 1R.
                #
                # A same-bar TP is still booked, because entry -> trigger -> TP is a
                # determinable order. Bars where the outcome was genuinely
                # unresolvable are counted as ambiguous so the exposure is measured
                # rather than assumed away.
                sl = t.entry_price
                if side is Side.LONG:
                    gap_tp = tp > 0 and o >= tp
                    hit_tp = tp > 0 and h >= tp
                    unresolved = l <= sl
                else:
                    gap_tp = tp > 0 and o <= tp
                    hit_tp = tp > 0 and l <= tp
                    unresolved = h >= sl
                if gap_tp:
                    # Gapped clean past the target: both legs fill at the open.
                    return (partial_px, o, EXIT_TP, False)
                if hit_tp:
                    return (partial_px, tp, EXIT_TP, unresolved)
                return (partial_px, None, "", unresolved)

        px, reason = self._resolve_stops(side, sl, tp, o, h, l)
        if px is not None and reason == EXIT_SL and t.be_moved:
            reason = EXIT_BE
        return (partial_px, px, reason or "", ambiguous)

    def _fire_partial(self, t, side, px, i, ts, spec, spread_pts):
        """Bank the scale-out leg and pull the stop to entry. Returns cash banked.

        The partial volume is derived here rather than at open, because it must be
        legal against the symbol's volume_step and must leave a runnable remainder.
        If the position is too small to split, the break-even move still happens --
        halving a 0.01-lot position is not possible at any broker, and silently
        skipping the whole rule in that case would be worse than skipping half of it.
        """
        t.be_moved = True
        # Exactly the entry, not a tick-rounded version of it: _resolve_bar tests
        # the same number, and a half-tick disagreement between the level being
        # tested and the level being filled is how off-by-one exits get born.
        t.sl_price = t.entry_price

        want = t.volume * t.partial_fraction
        vol = spec.round_volume(want) if want > 0 else 0.0
        remainder = round(t.volume - vol, 8)
        if vol <= 0 or remainder < spec.volume_min:
            return 0.0

        fill = self.costs.exit_fill(side, px, spec, spread_pts)
        t.partial_volume = vol
        t.partial_price = fill
        t.partial_index = i
        t.partial_time = ts.to_pydatetime()
        t.partial_pl = spec.pl(t.entry_price, fill, side, vol)
        t.remaining_volume = remainder
        return t.partial_pl

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

            # --- intrabar stops on the CURRENT bar (rules 3-6, 9) ---
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
                    partial_px, px, reason, amb = self._resolve_bar(
                        open_trade, position.side, o[i], hi[i], lo[i])
                    if amb and not both_in:
                        ambiguous_bars += 1
                    if partial_px is not None:
                        balance += self._fire_partial(
                            open_trade, position.side, partial_px, i, idx[i],
                            spec, spr[i])
                        position = PositionView(
                            ticket=position.ticket, symbol=position.symbol,
                            side=position.side, volume=open_trade.remaining_volume,
                            entry_price=position.entry_price,
                            entry_time=position.entry_time,
                            sl=open_trade.sl_price, tp=open_trade.tp_price)
                else:
                    px, reason = self._legacy_stops(open_trade, position.side, cl[i])

                if px is not None:
                    fill = self.costs.exit_fill(
                        position.side, px, spec, spr[i],
                        is_stop=(reason in (EXIT_SL, EXIT_BE)))
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
        be_d = sig.be_trigger_distance or 0.0
        sl = price - sl_d * side.sign if sl_d else 0.0
        tp = price + tp_d * side.sign if tp_d else 0.0
        # A trigger at or beyond the target can never fire before the TP resolves
        # the trade, and one at or below zero is meaningless: treat both as disabled
        # rather than carrying a level that silently never arms.
        if be_d > 0 and tp_d > 0 and be_d >= tp_d:
            be_d = 0.0
        be = price + be_d * side.sign if be_d > 0 else 0.0
        f = sig.features or {}
        t = TradeRecord(
            trade_id=trade_id, symbol=spec.name, side=side.value,
            entry_time=ts.to_pydatetime(), entry_index=i, entry_price=price,
            volume=self.cfg.volume, sl_price=spec.round_price(sl) if sl else 0.0,
            tp_price=spec.round_price(tp) if tp else 0.0, r_price=sl_d,
            remaining_volume=self.cfg.volume,
            be_trigger_price=spec.round_price(be) if be else 0.0,
            partial_fraction=max(0.0, min(1.0, sig.partial_fraction or 0.0)),
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
                           volume=t.remaining_volume, entry_price=price,
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
        """Close the remaining leg. Returns the CASH DELTA, not the trade's P&L.

        `partial_pl` was already added to the balance on the bar the scale-out
        fired, so returning `net_pl` here would bank it twice. The ledger row still
        reports `net_pl` as the whole trade, which is what analysis wants.
        """
        side = Side.LONG if t.side == "long" else Side.SHORT
        t.exit_price = price
        t.exit_index = i
        t.exit_time = ts.to_pydatetime()
        t.exit_reason = reason
        t.bars_held = i - t.entry_index
        t.duration_s = (t.exit_time - t.entry_time).total_seconds()
        remainder_gross = spec.pl(t.entry_price, price, side, t.remaining_volume)
        t.gross_pl = t.partial_pl + remainder_gross
        # Round turn on the volume OPENED: both legs eventually close, so the total
        # closed volume equals `volume` however many pieces it left in.
        t.commission = self.costs.commission(t.volume)
        t.swap = self.costs.swap(side, t.remaining_volume, t.entry_time,
                                 t.exit_time, spec)
        if t.partial_volume and t.partial_time is not None:
            t.swap += self.costs.swap(side, t.partial_volume, t.entry_time,
                                      t.partial_time, spec)
        t.net_pl = t.gross_pl - t.commission + t.swap
        if t.r_price > 0:
            # 1R stays the risk taken at ENTRY, on the volume opened. Re-basing it
            # on the surviving remainder would make a scaled-out trade look like it
            # risked less than it did, and inflate every R-multiple after a partial.
            risk = abs(spec.pl(t.entry_price, t.entry_price - t.r_price * side.sign,
                               side, t.volume))
            t.pnl_r = t.net_pl / risk if risk else 0.0
            t.mae_r = t.mae_price / t.r_price
            t.mfe_r = t.mfe_price / t.r_price
        return remainder_gross - t.commission + t.swap

    def _unrealised(self, t, pos, close, spec):
        if t is None or pos is None:
            return 0.0
        return spec.pl(t.entry_price, close, pos.side, t.remaining_volume)


def _strategy_cfg(strategy):
    cfg = getattr(strategy, "cfg", None)
    if cfg is None:
        return {}
    try:
        from dataclasses import asdict
        return asdict(cfg)
    except Exception:
        return dict(getattr(cfg, "__dict__", {}))
