import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  BacktestRun,
  BacktestSizing,
  deleteBacktest,
  getBacktests,
  getSettings,
  runBacktest,
} from './api';
import { PreferencesState } from './usePreferences';

// The symbol list is NOT hardcoded any more. It comes from /settings, which is
// keyed by whatever is in SYMBOL_CONFIG, so adding a third symbol to the backend
// puts it on this form with no frontend change -- the previous constant here was
// the one place a new symbol had to be remembered twice.
const FALLBACK_SYMBOLS = ['XAUUSDm'];

// Every field on this form is persisted through /preferences, and every run --
// inputs, outputs and errors alike -- is stored in `backtest_runs`. The form
// used to reset to a hardcoded 0.1 and a blank start date on every reload, and
// the result of a run that took real time to compute was gone the moment the
// page was navigated away from.
const PREF_KEY = 'backtest';

interface FormState {
  symbols: string[];
  start_date: string;
  end_date: string;
  initial_balance: number;
  // Keyed by symbol, because a lot is not a comparable unit across symbols: 0.1
  // lots of gold and 0.1 of Bitcoin risk about the same $70 here only by
  // coincidence of the two contract sizes. Strings, so a half-typed "0." does
  // not become NaN under the user's cursor.
  lots: Record<string, string>;
  scale_outs: Record<string, string>;
  preset: 'week' | 'month' | 'year' | null;
  // Pre-multi-symbol shape, read once so a saved form survives this change.
  symbol?: string;
  lot?: string;
  scale_out?: string;
}

const today = () => new Date().toISOString().split('T')[0];

const formatDate = (date: Date) => {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
};

const money = (n: number | null | undefined) =>
  n === null || n === undefined ? '—' : `${n < 0 ? '-' : ''}$${Math.abs(n).toFixed(2)}`;

/** Read the stored form, migrating the single-symbol shape it used to have. */
const initialForm = (stored: Partial<FormState>): FormState => {
  const symbols =
    Array.isArray(stored.symbols) && stored.symbols.length
      ? stored.symbols
      : stored.symbol
      ? [stored.symbol]
      : [];
  // The old form stored one `lot` / `scale_out` pair with no symbol attached to
  // it. Carrying it onto the symbol it was typed for keeps a saved size rather
  // than silently reverting someone's chosen lots to the live setting.
  const lots = { ...(stored.lots || {}) };
  const scaleOuts = { ...(stored.scale_outs || {}) };
  if (stored.symbol) {
    if (stored.lot !== undefined && lots[stored.symbol] === undefined) {
      lots[stored.symbol] = stored.lot;
    }
    if (stored.scale_out !== undefined && scaleOuts[stored.symbol] === undefined) {
      scaleOuts[stored.symbol] = stored.scale_out;
    }
  }
  return {
    symbols,
    start_date: stored.start_date || '',
    end_date: stored.end_date || today(),
    initial_balance:
      typeof stored.initial_balance === 'number' && isFinite(stored.initial_balance)
        ? stored.initial_balance
        : 1000,
    lots,
    scale_outs: scaleOuts,
    preset: stored.preset ?? null,
  };
};

interface SymbolRow {
  symbol: string;
  lot: string;
  scaleOut: string;
  lotNum: number;
  outNum: number;
  lotOk: boolean;
  outOk: boolean;
  share: number;
  sizing: any;
  problem: string | null;
}

const BacktestPage = ({ prefs }: { prefs: PreferencesState }) => {
  const stored = prefs.get<Partial<FormState>>(PREF_KEY, {});

  const [form, setForm] = useState<FormState>(() => initialForm(stored));
  const [result, setResult] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sizing, setSizing] = useState<Record<string, any>>({});
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [symbolsError, setSymbolsError] = useState<string | null>(null);

  // One writer for both local state and the stored preference, so no field can
  // be changed without being persisted.
  //
  // `next` is built from `form` and NOT inside the setForm updater. React may
  // re-run an updater during a later render pass, and prefs.update() calls
  // setState on the Dashboard above -- so the side effect that persists the form
  // was firing mid-render of a different component ("Cannot update a component
  // while rendering a different component"). A state updater has to be pure;
  // this one now is, and every caller patches once per event.
  const patch = useCallback(
    (fields: Partial<FormState>) => {
      const next = { ...form, ...fields };
      setForm(next);
      prefs.update({ [PREF_KEY]: next });
    },
    [form, prefs],
  );

  const available = useMemo(() => {
    const keys = Object.keys(sizing).filter(s => sizing[s] && !sizing[s].error);
    return keys.length ? keys : FALLBACK_SYMBOLS;
  }, [sizing]);

  // Live sizing for every configured symbol, fetched once. It also decides which
  // symbols this form offers, so it is not tied to the current selection the way
  // the single-symbol version was.
  //
  // A failure here is REPORTED, not swallowed. The fallback list is one symbol,
  // so a silently-failed fetch renders as "this bot only trades gold" -- a
  // plausible, wrong page with nothing on it to suggest anything went missing.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const all = await getSettings();
        if (cancelled) return;
        setSizing(all);
        setSymbolsError(null);
      } catch (e: any) {
        if (cancelled) return;
        setSymbolsError(
          `${e?.message || 'Could not reach the backend.'} Showing ${FALLBACK_SYMBOLS.join(
            ', ',
          )} only — the symbol list comes from the backend.`,
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Seed the selection and any never-set lot field from the live settings. Only
  // fields the user has never set: overwriting a stored value on every mount
  // would undo a deliberate choice each time the page was reopened.
  useEffect(() => {
    if (!Object.keys(sizing).length) return;
    const seed: Partial<FormState> = {};
    const valid = form.symbols.filter(s => available.includes(s));
    if (!valid.length) seed.symbols = [available[0]];
    else if (valid.length !== form.symbols.length) seed.symbols = valid;

    const lots = { ...form.lots };
    const scaleOuts = { ...form.scale_outs };
    let touched = false;
    for (const symbol of seed.symbols || form.symbols) {
      const live = sizing[symbol];
      if (!live || live.error) continue;
      if (lots[symbol] === undefined || lots[symbol] === '') {
        lots[symbol] = String(live.lot_size);
        touched = true;
      }
      if (scaleOuts[symbol] === undefined || scaleOuts[symbol] === '') {
        scaleOuts[symbol] = String(live.scale_out_lots);
        touched = true;
      }
    }
    if (touched) {
      seed.lots = lots;
      seed.scale_outs = scaleOuts;
    }
    if (Object.keys(seed).length) patch(seed);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sizing, form.symbols]);

  // Filtered by the selected symbol while there is exactly one, and unfiltered
  // once several are. The backend matches a combined run on its symbols ARRAY,
  // so "XAUUSDm" still finds a gold+Bitcoin run -- it is a fact about gold.
  const historySymbol = form.symbols.length === 1 ? form.symbols[0] : undefined;

  const loadRuns = useCallback(async () => {
    try {
      const res = await getBacktests(historySymbol, 15);
      setRuns(res.runs);
      setHistoryError(null);
    } catch (e: any) {
      setHistoryError(e?.message || 'Could not load past runs.');
    }
  }, [historySymbol]);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  const rows: SymbolRow[] = form.symbols.map(symbol => {
    const lot = form.lots[symbol] ?? '';
    const scaleOut = form.scale_outs[symbol] ?? '';
    const lotNum = parseFloat(lot);
    const outNum = parseFloat(scaleOut);
    const lotOk = isFinite(lotNum) && lotNum > 0;
    const outOk = isFinite(outNum) && outNum >= 0 && (!lotOk || outNum < lotNum);
    let problem: string | null = null;
    if (!lotOk) problem = 'Lot size must be a positive number.';
    else if (!isFinite(outNum) || outNum < 0) problem = 'Scale-out lots cannot be negative.';
    else if (outNum >= lotNum) problem = 'Scale-out must be smaller than the lot size. Use 0 to turn it off.';
    return {
      symbol,
      lot,
      scaleOut,
      lotNum,
      outNum,
      lotOk,
      outOk,
      share: lotOk && outOk && outNum > 0 ? (outNum / lotNum) * 100 : 0,
      sizing: sizing[symbol],
      problem,
    };
  });

  const allSelected = available.length > 1 && available.every(s => form.symbols.includes(s));

  const toggleSymbol = (symbol: string) => {
    const has = form.symbols.includes(symbol);
    // Never empty: a run with no symbol is not a shorter run, it is no run, and
    // the backend refuses it. Keep the last one selected instead.
    if (has && form.symbols.length === 1) return;
    const next = has
      ? form.symbols.filter(s => s !== symbol)
      : [...available.filter(s => form.symbols.includes(s) || s === symbol)];
    patch({ symbols: next });
  };

  // One click for "every asset, on one account" -- the common case, and awkward
  // to reach by toggling chips once there are more than two. Deliberately
  // one-way: clicking it while everything is already selected does nothing,
  // because the opposite of "all" here would be "none", which is not a run.
  const selectAll = () => {
    if (allSelected) return;
    patch({ symbols: [...available] });
  };

  const setLot = (symbol: string, value: string) =>
    patch({ lots: { ...form.lots, [symbol]: value } });
  const setScaleOut = (symbol: string, value: string) =>
    patch({ scale_outs: { ...form.scale_outs, [symbol]: value } });

  const handleDatePreset = (period: 'week' | 'month' | 'year') => {
    const end = new Date();
    const start = new Date();

    if (period === 'week') start.setDate(end.getDate() - 7);
    else if (period === 'month') start.setMonth(end.getMonth() - 1);
    else if (period === 'year') start.setFullYear(end.getFullYear() - 1);

    patch({
      preset: period,
      start_date: formatDate(start),
      end_date: formatDate(end),
    });
  };

  const execute = async () => {
    if (!form.symbols.length) {
      setError('Select at least one symbol');
      return;
    }
    if (!form.start_date || !form.end_date) {
      setError('Please select both start and end dates');
      return;
    }
    const bad = rows.find(r => r.problem);
    if (bad) {
      setError(`${bad.symbol}: ${bad.problem}`);
      return;
    }

    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const payloadSizing: BacktestSizing[] = rows.map(r => ({
        symbol: r.symbol,
        lot_size: r.lotNum,
        scale_out_lots: r.outNum,
      }));
      const data = await runBacktest({
        symbols: form.symbols,
        start_date: `${form.start_date}T00:00:00`,
        end_date: `${form.end_date}T23:59:59`,
        initial_balance: form.initial_balance,
        sizing: payloadSizing,
      });
      setResult(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
      // The failed run is stored too, so the list is refreshed either way -- a
      // window with no bars is a fact about that window worth keeping.
      loadRuns();
    }
  };

  // Reloads a stored run into the form AND shows its result, so a past run can
  // be re-read without re-computing it.
  const reload = (run: BacktestRun) => {
    const symbols = (run.symbols?.length ? run.symbols : [run.symbol]).filter(s =>
      available.includes(s),
    );
    const lots = { ...form.lots };
    const scaleOuts = { ...form.scale_outs };
    for (const symbol of symbols) {
      const stored = run.sizing?.[symbol];
      // Fall back to the flat columns for runs stored before per-symbol sizing
      // existed; those were always single-symbol, so the scalars are theirs.
      const lot = stored?.lot_size ?? (symbols.length === 1 ? run.lot_size : null);
      const out = stored?.scale_out_lots ?? (symbols.length === 1 ? run.scale_out_lots : null);
      if (lot !== null && lot !== undefined) lots[symbol] = String(lot);
      if (out !== null && out !== undefined) scaleOuts[symbol] = String(out);
    }
    patch({
      symbols: symbols.length ? symbols : form.symbols,
      start_date: run.start_date.slice(0, 10),
      end_date: run.end_date.slice(0, 10),
      initial_balance: run.initial_balance,
      lots,
      scale_outs: scaleOuts,
      preset: null,
    });
    setResult(run.status === 'ok' ? run.result : null);
    setError(run.status === 'error' ? run.error : null);
  };

  const remove = async (id: number) => {
    try {
      await deleteBacktest(id);
      loadRuns();
    } catch (e: any) {
      setHistoryError(e?.message || 'Could not delete that run.');
    }
  };

  const combined = !!result?.combined;
  const perSymbol: Array<[string, any]> = result
    ? (result.symbols || Object.keys(result.per_symbol || {})).map((s: string) => [
        s,
        result.per_symbol?.[s],
      ])
    : [];

  return (
    <div className="backtest-container">
      <div className="backtest-card">
        <p className="eyebrow">Simulation</p>
        <h2>Strategy Backtester</h2>
        <p className="backtest-sub">Run the envelope strategy against historical price data before risking it live.</p>

        <div className="symbol-picker">
          <span className="symbol-picker-label">Symbols</span>
          <div className="symbol-chips">
            {available.length > 1 && (
              <button
                type="button"
                className={`symbol-chip all ${allSelected ? 'active' : ''}`}
                onClick={selectAll}
                aria-pressed={allSelected}
              >
                All assets
              </button>
            )}
            {available.map(s => (
              <button
                key={s}
                type="button"
                className={`symbol-chip ${form.symbols.includes(s) ? 'active' : ''}`}
                onClick={() => toggleSymbol(s)}
                aria-pressed={form.symbols.includes(s)}
              >
                {s}
              </button>
            ))}
          </div>
          {symbolsError && <p className="sizing-msg err">{symbolsError}</p>}
          <span className="field-hint">
            {form.symbols.length > 1 ? (
              <>
                Combined: {form.symbols.join(' + ')} are replayed onto <b>one</b> $
                {form.initial_balance} account in close-time order, so the drawdown is the
                merged curve's — not the per-symbol ones added together.
              </>
            ) : (
              `Pick more than one — or “All assets” — to backtest them together on a single account.`
            )}
          </span>
        </div>

        <div className="backtest-form">
          <div className="form-group">
            <label>Initial Balance ($)</label>
            <input
              type="number"
              value={form.initial_balance}
              onChange={e => patch({ initial_balance: parseFloat(e.target.value) })}
            />
          </div>

          <div className="form-group">
            <label>Start Date</label>
            <input
              type="date"
              value={form.start_date}
              onChange={e => patch({ start_date: e.target.value, preset: null })}
            />
          </div>

          <div className="form-group">
            <label>End Date</label>
            <input
              type="date"
              value={form.end_date}
              onChange={e => patch({ end_date: e.target.value, preset: null })}
            />
          </div>

          <div className="preset-group">
            {(['week', 'month', 'year'] as const).map(p => (
              <button
                key={p}
                onClick={() => handleDatePreset(p)}
                className={`preset-btn ${form.preset === p ? 'active' : ''}`}
              >
                {p === 'week' ? 'Last Week' : p === 'month' ? 'Last Month' : 'Last Year'}
              </button>
            ))}
          </div>
        </div>

        {/* One sizing row per selected symbol. Per symbol and not one shared
            pair, because the stop is what turns lots into dollars and it is not
            the same distance on two instruments -- gold's is 7.00, Bitcoin's is
            700.00. The dollar figure beside each row is the point. */}
        <div className="sizing-rows">
          {rows.map(r => {
            const step = r.sizing?.volume_step ?? 0.01;
            const trigger = r.sizing ? r.sizing.be_trigger_pips * r.sizing.pip : null;
            const risk = r.sizing && r.lotOk ? r.sizing.risk_per_lot * r.lotNum : null;
            return (
              <div className="sizing-row" key={r.symbol}>
                <div className="sizing-row-head">
                  <span className="sizing-row-symbol">{r.symbol}</span>
                  <span className="sizing-row-risk">
                    {risk !== null ? `~$${risk.toFixed(0)} at risk / trade` : '—'}
                  </span>
                </div>
                <div className="sizing-grid">
                  <label className="sizing-field">
                    <span>Lot size</span>
                    <input
                      type="number"
                      inputMode="decimal"
                      min={0}
                      step={step}
                      value={r.lot}
                      onChange={e => setLot(r.symbol, e.target.value)}
                    />
                  </label>
                  <label className="sizing-field">
                    <span>Scale-out lots</span>
                    <input
                      type="number"
                      inputMode="decimal"
                      min={0}
                      step={step}
                      value={r.scaleOut}
                      onChange={e => setScaleOut(r.symbol, e.target.value)}
                    />
                  </label>
                </div>
                <span className="field-hint">
                  {r.outOk && r.outNum > 0 && trigger !== null
                    ? `${r.share.toFixed(0)}% banked at +${trigger.toFixed(2)}, stop to break-even.`
                    : 'Scale-out off — the whole position runs to the stop or the target.'}
                  {r.sizing ? ` Live setting: ${r.sizing.lot_size} / ${r.sizing.scale_out_lots}.` : ''}
                </span>
                {r.problem && <p className="sizing-msg err">{r.problem}</p>}
              </div>
            );
          })}
          <p className="field-hint">
            The scale-out is measured <b>negative</b> on both cached symbols: it lifts the
            win rate and lowers expectancy. 0 turns it off.
          </p>
        </div>

        <button className="btn-run" onClick={execute} disabled={loading}>
          {loading ? 'Running...' : form.symbols.length > 1 ? 'Run Combined Backtest' : 'Run Backtest'}
        </button>

        {error && <div className="error-msg">{error}</div>}

        {result && (
          <>
            {combined && (
              <p className="result-note">
                Combined across <b>{(result.symbols || []).join(' + ')}</b> on one $
                {result.initial_balance?.toFixed(0)} account.
              </p>
            )}
            <div className="results-grid">
              <div className="result-card">
                <div className="res-label">Final Balance</div>
                <div className="res-value">${result.final_balance.toFixed(2)}</div>
              </div>
              <div className="result-card">
                <div className="res-label">Total P&L</div>
                <div className={`res-value ${result.total_pl >= 0 ? 'positive' : 'negative'}`}>
                  ${result.total_pl.toFixed(2)}
                </div>
              </div>
              <div className="result-card">
                <div className="res-label">Win Rate</div>
                <div className="res-value">{result.win_rate.toFixed(2)}%</div>
              </div>
              <div className="result-card">
                <div className="res-label">Trades</div>
                <div className="res-value">{result.trades_opened}</div>
              </div>
              <div className="result-card">
                <div className="res-label">Wins / Losses</div>
                <div className="res-value">{result.wins} / {result.losses}</div>
              </div>
              <div className="result-card">
                <div className="res-label">
                  {combined ? 'Max Drawdown (merged)' : 'Max Drawdown'}
                </div>
                <div className="res-value">{result.max_drawdown.toFixed(2)}%</div>
              </div>
              <div className="result-card">
                <div className="res-label">Scale-outs Fired</div>
                <div className="res-value">{result.partials_fired}</div>
              </div>
              <div className="result-card">
                <div className="res-label">Banked on Partials</div>
                <div className={`res-value ${result.partial_pl >= 0 ? 'positive' : 'negative'}`}>
                  ${result.partial_pl.toFixed(2)}
                </div>
              </div>
              {!combined && result.lot_size !== undefined && (
                <div className="result-card">
                  <div className="res-label">Lots (out / runner)</div>
                  <div className="res-value">
                    {result.lot_size} ({result.scale_out_lots} / {result.runner_lots})
                  </div>
                </div>
              )}
            </div>

            {/* Always shown, even for one symbol, so the combined and single
                views are the same view. On a combined run these numbers are each
                symbol run ALONE on the full starting balance -- which is why
                their drawdowns do not add up to the merged one above. */}
            <div className="table-scroll">
              <table className="runs-table per-symbol-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th className="num">Lots (out / runner)</th>
                    <th className="num">Trades</th>
                    <th className="num">Wins / Losses</th>
                    <th className="num">Win rate</th>
                    <th className="num">P&L</th>
                    <th className="num">Scale-outs</th>
                    <th className="num">Own drawdown</th>
                  </tr>
                </thead>
                <tbody>
                  {perSymbol.map(([symbol, per]) =>
                    per ? (
                      <tr key={symbol}>
                        <td className="mono">{symbol}</td>
                        <td className="num">
                          {per.lot_size} ({per.scale_out_lots} / {per.runner_lots})
                        </td>
                        <td className="num">{per.trades_opened}</td>
                        <td className="num">{per.wins} / {per.losses}</td>
                        <td className="num">{per.win_rate.toFixed(1)}%</td>
                        <td className={`num strong ${per.total_pl >= 0 ? 'positive' : 'negative'}`}>
                          {money(per.total_pl)}
                        </td>
                        <td className="num">{per.partials_fired}</td>
                        <td className="num">{per.max_drawdown.toFixed(1)}%</td>
                      </tr>
                    ) : null,
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}

        {/* The engine has always returned this and the page has always dropped it.
            It says the numbers above are optimistic, which is the single most
            important thing on the page. */}
        {result?.warning && <p className="result-note">{result.warning}</p>}

        <div className="run-history">
          <h3>Past runs</h3>
          <p className="backtest-sub">
            Every run is stored with the inputs that produced it — click one to load
            its parameters and result back into the form. <b>engine</b> matters:
            these are all the legacy close-only engine, whose numbers are
            systematically optimistic, so they are not comparable with a
            <code> run_baseline</code> report.
          </p>
          {historyError && <div className="error-msg">{historyError}</div>}
          {!runs.length ? (
            <p className="sizing-hint muted">No runs recorded yet.</p>
          ) : (
            <div className="table-scroll">
              <table className="runs-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Symbols</th>
                    <th>Window</th>
                    <th className="num">Balance</th>
                    <th className="num">P&L</th>
                    <th className="num">Win rate</th>
                    <th className="num">Trades</th>
                    <th>Engine</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {runs.map(run => {
                    const r = run.result || {};
                    return (
                      <tr key={run.id} className="run-row" onClick={() => reload(run)}>
                        <td>{new Date(run.created_at).toISOString().slice(0, 16).replace('T', ' ')}</td>
                        <td className="mono">{run.symbol}</td>
                        <td className="mono">
                          {run.start_date.slice(0, 10)} → {run.end_date.slice(0, 10)}
                        </td>
                        <td className="num">${run.initial_balance.toFixed(0)}</td>
                        <td className={`num ${run.status === 'error' ? '' : (r.total_pl ?? 0) >= 0 ? 'positive' : 'negative'}`}>
                          {run.status === 'error' ? '—' : `$${(r.total_pl ?? 0).toFixed(2)}`}
                        </td>
                        <td className="num">
                          {run.status === 'error' ? '—' : `${(r.win_rate ?? 0).toFixed(1)}%`}
                        </td>
                        <td className="num">{run.status === 'error' ? '—' : r.trades_opened ?? 0}</td>
                        <td>
                          {run.status === 'error' ? (
                            <span className="run-failed" title={run.error || ''}>failed</span>
                          ) : (
                            run.engine
                          )}
                        </td>
                        <td>
                          <button
                            className="link-btn danger"
                            onClick={e => {
                              e.stopPropagation();
                              remove(run.id);
                            }}
                          >
                            Delete
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default BacktestPage;
