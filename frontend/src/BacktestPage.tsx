import React, { useCallback, useEffect, useState } from 'react';
import { BacktestRun, deleteBacktest, getBacktests, getSettings, runBacktest } from './api';
import { PreferencesState } from './usePreferences';

const SUPPORTED_SYMBOLS = ['XAUUSDm'];

// Every field on this form is persisted through /preferences, and every run --
// inputs, outputs and errors alike -- is stored in `backtest_runs`. The form
// used to reset to a hardcoded 0.1 and a blank start date on every reload, and
// the result of a run that took real time to compute was gone the moment the
// page was navigated away from.
const PREF_KEY = 'backtest';

interface FormState {
  symbol: string;
  start_date: string;
  end_date: string;
  initial_balance: number;
  lot: string;
  scale_out: string;
  preset: 'week' | 'month' | 'year' | null;
}

const today = () => new Date().toISOString().split('T')[0];

const formatDate = (date: Date) => {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
};

const BacktestPage = ({ prefs }: { prefs: PreferencesState }) => {
  const stored = prefs.get<Partial<FormState>>(PREF_KEY, {});

  const [form, setForm] = useState<FormState>({
    symbol: stored.symbol && SUPPORTED_SYMBOLS.includes(stored.symbol) ? stored.symbol : 'XAUUSDm',
    start_date: stored.start_date || '',
    end_date: stored.end_date || today(),
    initial_balance:
      typeof stored.initial_balance === 'number' && isFinite(stored.initial_balance)
        ? stored.initial_balance
        : 1000,
    // Empty string, not '0.1': the live sizing is fetched below and seeds these,
    // so an untouched form backtests the bot AS IT STANDS rather than a
    // hardcoded default that may be nothing like the configured size.
    lot: stored.lot ?? '',
    scale_out: stored.scale_out ?? '',
    preset: stored.preset ?? null,
  });

  const [result, setResult] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sizing, setSizing] = useState<any>(null);
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [historyError, setHistoryError] = useState<string | null>(null);

  // One writer for both local state and the stored preference, so no field can
  // be changed without being persisted.
  const patch = useCallback(
    (fields: Partial<FormState>) => {
      setForm(prev => {
        const next = { ...prev, ...fields };
        prefs.update({ [PREF_KEY]: next });
        return next;
      });
    },
    [prefs],
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const all = await getSettings();
        const live = all[form.symbol];
        if (cancelled || !live || live.error) return;
        setSizing(live);
        // Only seed fields the user has never set. Overwriting a stored value
        // with the live sizing on every mount would undo a deliberate choice
        // every time the page was reopened.
        const seed: Partial<FormState> = {};
        if (form.lot === '') seed.lot = String(live.lot_size);
        if (form.scale_out === '') seed.scale_out = String(live.scale_out_lots);
        if (Object.keys(seed).length) patch(seed);
      } catch (e) {
        // Leave the defaults in place; the run itself will report a dead backend.
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.symbol]);

  const loadRuns = useCallback(async () => {
    try {
      const res = await getBacktests(form.symbol, 15);
      setRuns(res.runs);
      setHistoryError(null);
    } catch (e: any) {
      setHistoryError(e?.message || 'Could not load past runs.');
    }
  }, [form.symbol]);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  const lotNum = parseFloat(form.lot);
  const outNum = parseFloat(form.scale_out);
  const lotOk = isFinite(lotNum) && lotNum > 0;
  const outOk = isFinite(outNum) && outNum >= 0 && (!lotOk || outNum < lotNum);
  const share = lotOk && outOk && outNum > 0 ? (outNum / lotNum) * 100 : 0;
  const step = sizing?.volume_step ?? 0.01;
  const triggerPrice = sizing ? sizing.be_trigger_pips * sizing.pip : 5;

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
    if (!form.start_date || !form.end_date) {
      setError('Please select both start and end dates');
      return;
    }
    if (!lotOk) {
      setError('Lot size must be a positive number');
      return;
    }
    if (!outOk) {
      setError('Scale-out lots must be 0 or more, and smaller than the lot size');
      return;
    }

    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const data = await runBacktest({
        symbol: form.symbol,
        start_date: `${form.start_date}T00:00:00`,
        end_date: `${form.end_date}T23:59:59`,
        initial_balance: form.initial_balance,
        lot_size: lotNum,
        scale_out_lots: outNum,
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
    patch({
      symbol: run.symbol,
      start_date: run.start_date.slice(0, 10),
      end_date: run.end_date.slice(0, 10),
      initial_balance: run.initial_balance,
      lot: run.lot_size !== null ? String(run.lot_size) : form.lot,
      scale_out: run.scale_out_lots !== null ? String(run.scale_out_lots) : form.scale_out,
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

  return (
    <div className="backtest-container">
      <div className="backtest-card">
        <p className="eyebrow">Simulation</p>
        <h2>Strategy Backtester</h2>
        <p className="backtest-sub">Run the envelope strategy against historical price data before risking it live.</p>

        <div className="backtest-form">
          <div className="form-group">
            <label>Symbol</label>
            <select value={form.symbol} onChange={e => patch({ symbol: e.target.value })}>
              {SUPPORTED_SYMBOLS.map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          <div className="form-group">
            <label>Initial Balance ($)</label>
            <input
              type="number"
              value={form.initial_balance}
              onChange={e => patch({ initial_balance: parseFloat(e.target.value) })}
            />
          </div>

          <div className="form-group">
            <label>Lot size</label>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              step={step}
              value={form.lot}
              onChange={e => patch({ lot: e.target.value })}
            />
            <span className="field-hint">
              Scales P&L linearly. Live setting: {sizing ? sizing.lot_size : '—'}
            </span>
          </div>

          <div className="form-group">
            <label>Scale-out lots</label>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              step={step}
              value={form.scale_out}
              onChange={e => patch({ scale_out: e.target.value })}
            />
            <span className="field-hint">
              {outOk && outNum > 0
                ? `${share.toFixed(0)}% banked at +${triggerPrice.toFixed(2)}, stop to break-even.`
                : 'Off — the whole position runs to the stop or the target.'}
              {' '}Measured NEGATIVE on cached gold data: it lifts the win rate and
              lowers expectancy. 0 turns it off.
            </span>
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

          <button className="btn-run" onClick={execute} disabled={loading}>
            {loading ? 'Running...' : 'Run Backtest'}
          </button>
        </div>

        {error && <div className="error-msg">{error}</div>}

        {result && (
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
              <div className="res-label">Max Drawdown</div>
              <div className="res-value">{result.max_drawdown.toFixed(2)}%</div>
            </div>
            <div className="result-card">
              <div className="res-label">Lots (out / runner)</div>
              <div className="res-value">
                {result.lot_size} ({result.scale_out_lots} / {result.runner_lots})
              </div>
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
          </div>
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
                    <th>Window</th>
                    <th className="num">Balance</th>
                    <th className="num">Lots</th>
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
                        <td className="mono">
                          {run.start_date.slice(0, 10)} → {run.end_date.slice(0, 10)}
                        </td>
                        <td className="num">${run.initial_balance.toFixed(0)}</td>
                        <td className="num">
                          {run.lot_size ?? '—'}
                          {run.scale_out_lots ? ` / ${run.scale_out_lots}` : ''}
                        </td>
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
