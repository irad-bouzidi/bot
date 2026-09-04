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
import ConfirmDialog from './ConfirmDialog';

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
  // The run the delete button was pressed on, held until it is confirmed. A
  // stored run is the only record of a computation that took real time, and the
  // button sits in a row that is itself clickable -- so the press has to be a
  // request to delete, not the deletion.
  const [pendingDelete, setPendingDelete] = useState<BacktestRun | null>(null);
  const [deleting, setDeleting] = useState(false);

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

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    setHistoryError(null);
    try {
      await deleteBacktest(pendingDelete.id);
      setPendingDelete(null);
      loadRuns();
    } catch (e: any) {
      // The dialog closes either way: left open, it would be covering the
      // message that says why the delete did not happen.
      setPendingDelete(null);
      setHistoryError(e?.message || 'Could not delete that run.');
    } finally {
      setDeleting(false);
    }
  };

  /** Names one run in prose, for the delete button and the dialog. */
  const runLabel = (run: BacktestRun) =>
    run.symbol +
    ', ' +
    run.start_date.slice(0, 10) +
    ' to ' +
    run.end_date.slice(0, 10) +
    ', run ' +
    new Date(run.created_at).toISOString().slice(0, 16).replace('T', ' ');

  const combined = !!result?.combined;
  const perSymbol: Array<[string, any]> = result
    ? (result.symbols || Object.keys(result.per_symbol || {})).map((s: string) => [
        s,
        result.per_symbol?.[s],
      ])
    : [];

  return (
    <>
      <div className="page-head">
        <div>
          <p className="eyebrow">Simulation</p>
          <h2 className="page-title">Strategy backtester</h2>
          <p className="page-desc">
            Run the envelope strategy against historical price data before risking it
            live. Every run is stored with the inputs that produced it.
          </p>
        </div>
      </div>

      {/* The form is one card in three blocks -- instruments, window, sizing --
          because they are answered in that order and the third one's fields
          depend on the first one's answer. */}
      <section className="card">
        <div className="card-header">
          <div>
            <h3 className="card-title">Parameters</h3>
            <p className="card-desc">
              Sizing is per symbol: a lot is not a comparable unit across instruments,
              because the stop is what turns lots into dollars and it is 7.00 on gold
              against 700.00 on Bitcoin.
            </p>
          </div>
        </div>

        <div className="card-content">
          <div className="field wide">
            <span className="field-label">Instruments</span>
            <div className="chip-group">
              {available.length > 1 && (
                <button
                  type="button"
                  className={`chip chip-all ${allSelected ? 'active' : ''}`}
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
                  className={`chip ${form.symbols.includes(s) ? 'active' : ''}`}
                  onClick={() => toggleSymbol(s)}
                  aria-pressed={form.symbols.includes(s)}
                >
                  {s}
                </button>
              ))}
            </div>
            {symbolsError && <p className="msg err">{symbolsError}</p>}
            <p className="hint">
              {form.symbols.length > 1 ? (
                <>
                  Combined: {form.symbols.join(' + ')} are replayed onto <b>one</b> $
                  {form.initial_balance} account in close-time order, so the drawdown is the
                  merged curve's — not the per-symbol ones added together.
                </>
              ) : (
                'Pick more than one — or “All assets” — to backtest them together on a single account.'
              )}
            </p>
          </div>

          <hr className="separator" />

          <div className="form-grid three">
            <label className="field">
              <span>Initial balance ($)</span>
              <input
                className="input"
                type="number"
                value={form.initial_balance}
                onChange={e => patch({ initial_balance: parseFloat(e.target.value) })}
              />
            </label>

            <label className="field">
              <span>Start date</span>
              <input
                className="input"
                type="date"
                value={form.start_date}
                onChange={e => patch({ start_date: e.target.value, preset: null })}
              />
            </label>

            <label className="field">
              <span>End date</span>
              <input
                className="input"
                type="date"
                value={form.end_date}
                onChange={e => patch({ end_date: e.target.value, preset: null })}
              />
            </label>
          </div>

          <div className="field wide">
            <span className="field-label">Quick window</span>
            <div className="toggle-group">
              {(['week', 'month', 'year'] as const).map(p => (
                <button
                  key={p}
                  type="button"
                  onClick={() => handleDatePreset(p)}
                  className={`toggle ${form.preset === p ? 'active' : ''}`}
                  aria-pressed={form.preset === p}
                >
                  {p === 'week' ? 'Last Week' : p === 'month' ? 'Last Month' : 'Last Year'}
                </button>
              ))}
            </div>
          </div>

          <hr className="separator" />

          {/* One sizing row per selected symbol. Per symbol and not one shared
              pair, because the stop is what turns lots into dollars and it is not
              the same distance on two instruments -- gold's is 7.00, Bitcoin's is
              700.00. The dollar figure beside each row is the point. */}
          <div className="field wide">
            <span className="field-label">Position sizing</span>
            <div className="stack">
              {rows.map(r => {
                const step = r.sizing?.volume_step ?? 0.01;
                const trigger = r.sizing ? r.sizing.be_trigger_pips * r.sizing.pip : null;
                const risk = r.sizing && r.lotOk ? r.sizing.risk_per_lot * r.lotNum : null;
                return (
                  <div className="panel" key={r.symbol}>
                    <div className="panel-head">
                      <span className="panel-title">{r.symbol}</span>
                      <span className="panel-meta">
                        {risk !== null ? `~$${risk.toFixed(0)} at risk / trade` : '—'}
                      </span>
                    </div>
                    <div className="form-grid two">
                      <label className="field">
                        <span>Lot size</span>
                        <input
                          className="input"
                          type="number"
                          inputMode="decimal"
                          min={0}
                          step={step}
                          value={r.lot}
                          onChange={e => setLot(r.symbol, e.target.value)}
                        />
                      </label>
                      <label className="field">
                        <span>Scale-out lots</span>
                        <input
                          className="input"
                          type="number"
                          inputMode="decimal"
                          min={0}
                          step={step}
                          value={r.scaleOut}
                          onChange={e => setScaleOut(r.symbol, e.target.value)}
                        />
                      </label>
                    </div>
                    <p className="hint">
                      {r.outOk && r.outNum > 0 && trigger !== null
                        ? `${r.share.toFixed(0)}% banked at +${trigger.toFixed(2)}, stop to break-even.`
                        : 'Scale-out off — the whole position runs to the stop or the target.'}
                      {r.sizing ? ` Live setting: ${r.sizing.lot_size} / ${r.sizing.scale_out_lots}.` : ''}
                    </p>
                    {r.problem && <p className="msg err">{r.problem}</p>}
                  </div>
                );
              })}
            </div>
            <p className="hint">
              The scale-out is measured <b>negative</b> on both cached symbols: it lifts the
              win rate and lowers expectancy. 0 turns it off.
            </p>
          </div>
        </div>

        <div className="card-footer">
          <button className="btn btn-primary" onClick={execute} disabled={loading}>
            {loading ? 'Running…' : form.symbols.length > 1 ? 'Run Combined Backtest' : 'Run Backtest'}
          </button>
          <span className="hint">
            {form.start_date && form.end_date
              ? `${form.start_date} → ${form.end_date}`
              : 'Pick a window to run.'}
          </span>
        </div>
      </section>

      {error && (
        <div className="alert alert-destructive">
          <span className="alert-icon">⚠️</span>
          <div className="alert-body">
            <p>{error}</p>
          </div>
        </div>
      )}

      {result && (
        <section className="card">
          <div className="card-header">
            <div>
              <h3 className="card-title">Result</h3>
              <p className="card-desc">
                {combined ? (
                  <>
                    Combined across <b>{(result.symbols || []).join(' + ')}</b> on one $
                    {result.initial_balance?.toFixed(0)} account.
                  </>
                ) : (
                  'Legacy close-only engine — see the note under the table.'
                )}
              </p>
            </div>
          </div>

          <div className="card-content">
            <div className="stat-grid">
              <div className="stat">
                <span className="stat-label">Final balance</span>
                <span className="stat-value">${result.final_balance.toFixed(2)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Total P&L</span>
                <span className={`stat-value ${result.total_pl >= 0 ? 'positive' : 'negative'}`}>
                  {money(result.total_pl)}
                </span>
              </div>
              <div className="stat">
                <span className="stat-label">Win rate</span>
                <span className="stat-value">{result.win_rate.toFixed(2)}%</span>
              </div>
              <div className="stat">
                <span className="stat-label">Trades</span>
                <span className="stat-value">{result.trades_opened}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Wins / losses</span>
                <span className="stat-value">{result.wins} / {result.losses}</span>
              </div>
              <div className="stat">
                <span className="stat-label">
                  {combined ? 'Max drawdown (merged)' : 'Max drawdown'}
                </span>
                <span className="stat-value">{result.max_drawdown.toFixed(2)}%</span>
              </div>
              <div className="stat">
                <span className="stat-label">Scale-outs fired</span>
                <span className="stat-value">{result.partials_fired}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Banked on partials</span>
                <span className={`stat-value ${result.partial_pl >= 0 ? 'positive' : 'negative'}`}>
                  {money(result.partial_pl)}
                </span>
              </div>
              {!combined && result.lot_size !== undefined && (
                <div className="stat">
                  <span className="stat-label">Lots (out / runner)</span>
                  <span className="stat-value">
                    {result.lot_size} ({result.scale_out_lots} / {result.runner_lots})
                  </span>
                </div>
              )}
            </div>

            {/* The engine has always returned this and the page has always dropped it.
                It says the numbers above are optimistic, which is the single most
                important thing on the page. */}
            {result.warning && <p className="note">{result.warning}</p>}
          </div>
        </section>
      )}

      {/* Always shown, even for one symbol, so the combined and single views are
          the same view. On a combined run these numbers are each symbol run
          ALONE on the full starting balance -- which is why their drawdowns do
          not add up to the merged one above.

          Its own card, flush to the edges, because that is how every other
          table in this app sits: a card header names it and the table starts at
          the border. It used to be a bordered box inset in the card above,
          which made the same component look like two different ones on two
          pages. */}
      {result && (
        <section className="card">
          <div className="card-header">
            <div>
              <h3 className="card-title">Per symbol</h3>
              <p className="card-desc">
                Each symbol run <b>alone</b> on the full ${result.initial_balance?.toFixed(0)}{' '}
                starting balance. Their drawdowns are therefore not the merged figure
                above, and do not add up to it.
              </p>
            </div>
          </div>
          <div className="table-wrap">
            <table className="data-table">
              <caption className="sr-only">
                Backtest result for each symbol, run separately
              </caption>
              <thead>
                <tr>
                  <th scope="col">Symbol</th>
                  <th scope="col" className="num">Lots (out / runner)</th>
                  <th scope="col" className="num">Trades</th>
                  <th scope="col" className="num">Wins / losses</th>
                  <th scope="col" className="num">Win rate</th>
                  <th scope="col" className="num">P&L</th>
                  <th scope="col" className="num">Scale-outs</th>
                  <th scope="col" className="num">Own drawdown</th>
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
        </section>
      )}

      <section className="card">
        <div className="card-header">
          <div>
            <h3 className="card-title">Past runs</h3>
            <p className="card-desc">
              Select a row to load its parameters and result back into the form.{' '}
              <b>Engine</b> matters: these are all the legacy close-only engine, whose
              numbers are systematically optimistic, so they are not comparable with a{' '}
              <code>run_baseline</code> report.
            </p>
          </div>
          {historySymbol && <span className="badge badge-outline">{historySymbol}</span>}
        </div>

        {historyError && (
          <div className="card-content">
            <p className="msg err">{historyError}</p>
          </div>
        )}

        {!runs.length ? (
          <div className="empty-state">
            <div className="empty-icon">🗒️</div>
            <h3>No runs recorded yet</h3>
            <p>Run a backtest above and it will be stored here with its inputs.</p>
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <caption className="sr-only">
                Stored backtest runs. Activate a row to load its parameters and result
                back into the form.
              </caption>
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Symbols</th>
                  <th scope="col">Window</th>
                  <th scope="col" className="num">Balance</th>
                  <th scope="col" className="num">P&L</th>
                  <th scope="col" className="num">Win rate</th>
                  <th scope="col" className="num">Trades</th>
                  <th scope="col">Engine</th>
                  {/* Not an empty <th>. A column with no name is announced as
                      blank, and this one holds the only destructive control on
                      the page. */}
                  <th scope="col" className="col-actions">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {runs.map(run => {
                  const r = run.result || {};
                  return (
                    <tr
                      key={run.id}
                      className="row-clickable"
                      // The row is the control that loads the run, so it has to
                      // be operable without a mouse. It was click-only, which
                      // left every stored run unreachable from the keyboard.
                      tabIndex={0}
                      onClick={() => reload(run)}
                      onKeyDown={e => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          reload(run);
                        }
                      }}
                    >
                      <td>{new Date(run.created_at).toISOString().slice(0, 16).replace('T', ' ')}</td>
                      <td className="mono">{run.symbol}</td>
                      <td className="mono">
                        {run.start_date.slice(0, 10)} → {run.end_date.slice(0, 10)}
                      </td>
                      <td className="num">${run.initial_balance.toFixed(0)}</td>
                      <td className={`num ${run.status === 'error' ? '' : (r.total_pl ?? 0) >= 0 ? 'positive' : 'negative'}`}>
                        {run.status === 'error' ? '—' : money(r.total_pl ?? 0)}
                      </td>
                      <td className="num">
                        {run.status === 'error' ? '—' : `${(r.win_rate ?? 0).toFixed(1)}%`}
                      </td>
                      <td className="num">{run.status === 'error' ? '—' : r.trades_opened ?? 0}</td>
                      <td>
                        {run.status === 'error' ? (
                          <span className="badge badge-destructive" title={run.error || ''}>
                            failed
                          </span>
                        ) : (
                          <span className="badge badge-secondary">{run.engine}</span>
                        )}
                      </td>
                      <td className="col-actions">
                        <div className="row-actions">
                          <button
                            type="button"
                            className="btn btn-icon danger"
                            // Icon-only, so it carries its own name -- and the
                            // name says WHICH run, because "Delete" repeated
                            // fifteen times down a column identifies nothing.
                            aria-label={'Delete the backtest run: ' + runLabel(run)}
                            title="Delete this run"
                            onClick={e => {
                              e.stopPropagation();
                              setPendingDelete(run);
                            }}
                          >
                            <svg viewBox="0 0 24 24" aria-hidden="true">
                              <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6" />
                            </svg>
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <ConfirmDialog
        open={!!pendingDelete}
        title="Delete this backtest run?"
        description={
          <>
            <p>
              <b>{pendingDelete ? runLabel(pendingDelete) : ''}</b>
            </p>
            <p>
              The stored inputs and result are removed for good. Re-running the same
              window will recompute it, but this record of it will not come back.
            </p>
          </>
        }
        confirmLabel="Delete run"
        tone="destructive"
        busy={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </>
  );
};

export default BacktestPage;
