import React, { useCallback, useEffect, useState } from 'react';
import './App.css';
import BacktestPage from './BacktestPage';
import TradesPage from './TradesPage';
import {
  ApiError,
  Health,
  SettingsChange,
  controlBot,
  getHealth,
  getSettings,
  getSettingsHistory,
  getStats,
  saveSizing,
} from './api';
import { usePreferences } from './usePreferences';

const Skeleton = ({ className = '' }: { className?: string }) => (
  <div className={`skeleton ${className}`} />
);

type View = 'dashboard' | 'backtest' | 'trades';
const VIEWS: View[] = ['dashboard', 'backtest', 'trades'];
const VIEW_LABELS: Record<View, string> = {
  dashboard: 'Dashboard',
  backtest: 'Backtest',
  trades: 'Trades',
};

// Signature device: a stylized Nadaraya-Watson kernel-regression envelope —
// the exact smoothed mean + upper/lower bands these bots trade against.
const EnvelopeCurve = () => (
  <svg className="envelope-curve" viewBox="0 0 1200 100" preserveAspectRatio="none" aria-hidden="true">
    <line className="env-baseline" x1="0" y1="99" x2="1200" y2="99" />
    <path
      className="env-band"
      d="M0,32 C100,2 200,2 300,32 C400,62 500,62 600,32 C700,2 800,2 900,32 C1000,62 1100,62 1200,32"
    />
    <path
      className="env-band"
      d="M0,68 C100,38 200,38 300,68 C400,98 500,98 600,68 C700,38 800,38 900,68 C1000,98 1100,98 1200,68"
    />
    <path
      className="env-mean"
      d="M0,50 C100,20 200,20 300,50 C400,80 500,80 600,50 C700,20 800,20 900,50 C1000,80 1100,80 1200,50"
    />
  </svg>
);

const StatCardSkeleton = () => (
  <div className="stat-card">
    <Skeleton className="stat-label-skeleton" />
    <Skeleton className="stat-value-skeleton" />
  </div>
);

const BotCardSkeleton = () => (
  <div className="bot-card">
    <div className="bot-header">
      <Skeleton className="bot-title-skeleton" />
      <Skeleton className="status-badge-skeleton" />
    </div>
    <div className="indicator-grid">
      <Skeleton /><Skeleton /><Skeleton /><Skeleton />
    </div>
    <div className="performance-grid">
      <Skeleton /><Skeleton /><Skeleton /><Skeleton />
    </div>
    <div className="sizing-section">
      <Skeleton className="sizing-title-skeleton" />
      <div className="sizing-grid">
        <Skeleton className="sizing-field-skeleton" />
        <Skeleton className="sizing-field-skeleton" />
      </div>
    </div>
    <div className="button-group">
      <Skeleton className="btn-skeleton" /><Skeleton className="btn-skeleton" />
    </div>
  </div>
);

// The backend refuses to boot without Postgres -- it holds `lot_size`, the only
// risk control this bot has. But the database can go down AFTER boot, and then
// /stats answers with an `error` per bot instead of numbers. Say which of the
// two is broken and print the command that fixes it, rather than leaving the
// dashboard to show a plausible-looking flat, never-traded bot.
const DatabaseBanner = ({ health }: { health: Health | null }) => {
  if (!health || (health.database.reachable && health.database.tables_present)) return null;
  const { reachable, url, migrate_command, schema_version } = health.database;
  return (
    <div className="error-banner db-banner">
      <span>🗄️</span>
      <div>
        <p>
          {!reachable
            ? `Postgres at ${url} is not reachable. Nothing is being persisted — trade history, sizing changes and preferences are all being dropped.`
            : `Postgres is reachable but the schema is incomplete (version ${schema_version}).`}
        </p>
        <code className="db-banner-cmd">{migrate_command}</code>
      </div>
    </div>
  );
};

// The bot was running when the process went down and has not been restarted.
// Reported rather than acted on: restarting live trading with real money because
// a process came back up is not something an unauthenticated API should decide
// on its own, so the disagreement is surfaced and a human presses the button.
const ResumeNotice = ({
  symbol,
  onStart,
}: {
  symbol: string;
  onStart: () => void;
}) => (
  <div className="resume-notice">
    <span>
      <b>{symbol}</b> was running before the last shutdown and was not
      auto-started.
    </span>
    <button className="btn btn-start btn-inline" onClick={onStart}>
      Start again
    </button>
  </div>
);

const SizingHistory = ({ symbol }: { symbol: string }) => {
  const [rows, setRows] = useState<SettingsChange[] | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      const res = await getSettingsHistory(symbol, 10);
      setRows(res.history);
    } catch (e: any) {
      setError(e?.message || 'Could not load the sizing history.');
    }
  };

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && rows === null) load();
  };

  return (
    <div className="sizing-history">
      <button className="link-btn" onClick={toggle}>
        {open ? 'Hide' : 'Show'} sizing history
      </button>
      {open && (
        <>
          {error && <p className="sizing-msg err">{error}</p>}
          {rows && !rows.length && <p className="sizing-hint muted">No changes recorded.</p>}
          {rows && rows.length > 0 && (
            <ul className="history-list">
              {rows.map(r => (
                <li key={r.id}>
                  <span className="history-when">
                    {new Date(r.created_at).toISOString().slice(0, 16).replace('T', ' ')}
                  </span>
                  <span>
                    {r.prev_lot_size !== null && r.prev_lot_size !== r.lot_size
                      ? `${r.prev_lot_size} → ${r.lot_size} lots`
                      : `${r.lot_size} lots`}
                    {' · '}
                    {(r.partial_fraction * 100).toFixed(0)}% scale-out
                  </span>
                  <span className="history-source">{r.source}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
};

// Editing this changes the size of REAL orders, so two things are on the card and
// not buried: the dollar risk of whatever is currently typed, and the fact that the
// backend refuses the change outright while a position is open (it would otherwise
// re-scale a trade that is already running -- see BotManager.update_settings).
//
// Lots are what a trader types; the backend stores the scale-out as a SHARE of the
// position, so the share is echoed back under the field rather than left implicit.
const SizingEditor = ({ symbol, sizing, onSaved }: { symbol: string; sizing: any; onSaved: () => void }) => {
  const [lot, setLot] = useState(String(sizing.lot_size));
  const [scaleOut, setScaleOut] = useState(String(sizing.scale_out_lots));
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // The dashboard re-polls every 5s. Adopt the server's numbers only while the
  // user is NOT mid-edit, or a poll rewrites the digits under their cursor.
  useEffect(() => {
    if (dirty) return;
    setLot(String(sizing.lot_size));
    setScaleOut(String(sizing.scale_out_lots));
  }, [sizing.lot_size, sizing.scale_out_lots, dirty]);

  const lotNum = parseFloat(lot);
  const outNum = parseFloat(scaleOut);
  const lotOk = isFinite(lotNum) && lotNum > 0;
  const outOk = isFinite(outNum) && outNum >= 0 && (!lotOk || outNum < lotNum);
  const share = lotOk && outOk && outNum > 0 ? (outNum / lotNum) * 100 : 0;
  const locked = !!sizing.locked;
  const triggerPrice = (sizing.be_trigger_pips || 0) * (sizing.pip || 0);

  let problem: string | null = null;
  if (locked) problem = `${sizing.open_positions} open position(s) — sizing is locked until this trade closes.`;
  else if (!lotOk) problem = 'Lot size must be a positive number.';
  else if (!isFinite(outNum) || outNum < 0) problem = 'Scale-out lots cannot be negative.';
  else if (outNum >= lotNum) problem = 'Scale-out must be smaller than the lot size. Use 0 to turn it off.';

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const data = await saveSizing(symbol, lotNum, outNum);
      // Show what was actually applied, not what was typed: the backend snaps to
      // the broker's volume step, so 0.155 comes back as 0.16.
      setLot(String(data.lot_size));
      setScaleOut(String(data.scale_out_lots));
      setDirty(false);
      setMsg({ ok: true, text: data.notes?.length ? data.notes.join(' ') : 'Saved.' });
      onSaved();
    } catch (e: any) {
      // Covers the refusal cases as well as a dead backend: /settings answers
      // 200 with an { error } body when the edit is rejected, and api.ts turns
      // that into a throw so both land here with the server's own words.
      setMsg({ ok: false, text: e instanceof ApiError ? e.message : 'Could not reach the backend.' });
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    setLot(String(sizing.lot_size));
    setScaleOut(String(sizing.scale_out_lots));
    setDirty(false);
    setMsg(null);
  };

  const edit = (setter: (v: string) => void) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setter(e.target.value);
    setDirty(true);
    setMsg(null);
  };

  return (
    <div className="sizing-section">
      <div className="sizing-head">
        <span className="sizing-title">Position sizing</span>
        <span className={`sizing-risk ${lotOk ? '' : 'muted'}`}>
          {lotOk ? `~$${(sizing.risk_per_lot * lotNum).toFixed(0)} at risk / trade` : '—'}
        </span>
      </div>

      <div className="sizing-grid">
        <label className="sizing-field">
          <span>Lot size</span>
          <input
            type="number"
            inputMode="decimal"
            min={sizing.volume_min}
            max={sizing.volume_max}
            step={sizing.volume_step}
            value={lot}
            disabled={locked || busy}
            onChange={edit(setLot)}
          />
        </label>
        <label className="sizing-field">
          <span>Scale-out lots</span>
          <input
            type="number"
            inputMode="decimal"
            min={0}
            step={sizing.volume_step}
            value={scaleOut}
            disabled={locked || busy}
            onChange={edit(setScaleOut)}
          />
        </label>
      </div>

      <p className="sizing-hint">
        {outNum === 0 || !outOk ? (
          <>Scale-out off — the whole position runs to the stop or the target.</>
        ) : (
          <>
            <b>{share.toFixed(0)}%</b> banked at +{triggerPrice.toFixed(2)}, stop to break-even,{' '}
            {(lotNum - outNum).toFixed(2)} runs on. Stored as a share, so it stays {share.toFixed(0)}%
            if you change the lot size.
          </>
        )}
      </p>
      <p className="sizing-hint muted">
        Broker min {sizing.volume_min} · step {sizing.volume_step}
        {sizing.broker_limits ? '' : ' (terminal offline — defaults shown)'}
        {sizing.splittable === false && outNum > 0
          ? ` · ${lot} lots cannot be split here, so only the stop will move.`
          : ''}
      </p>

      {problem && <p className="sizing-msg err">{problem}</p>}
      {msg && <p className={`sizing-msg ${msg.ok ? 'ok' : 'err'}`}>{msg.text}</p>}

      <div className="sizing-actions">
        <button className="btn btn-save" onClick={save} disabled={!dirty || busy || !!problem}>
          {busy ? 'Saving…' : 'Save sizing'}
        </button>
        <button className="btn btn-reset" onClick={reset} disabled={!dirty || busy}>
          Reset
        </button>
      </div>

      <SizingHistory symbol={symbol} />
    </div>
  );
};

const Dashboard = () => {
  const [data, setData] = useState<any>(null);
  const [settings, setSettings] = useState<any>({});
  const [health, setHealth] = useState<Health | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Theme and the active view come from Postgres now, not localStorage. Nothing
  // the user chooses in this dashboard lives only in the browser.
  const prefs = usePreferences();
  const theme: 'light' | 'dark' = prefs.get<string>('theme', 'light') === 'dark' ? 'dark' : 'light';
  const storedView = prefs.get<View>('view', 'dashboard');
  const view: View = VIEWS.includes(storedView) ? storedView : 'dashboard';

  // Applied as an effect rather than in the click handler, so the stored value
  // is what paints on load -- the click only writes the preference.
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  const toggleTheme = () => prefs.update({ theme: theme === 'light' ? 'dark' : 'light' });
  const setView = (next: View) => prefs.update({ view: next });

  const fetchStats = useCallback(async () => {
    try {
      const json = await getStats();
      setData(json);
      setError(null);
    } catch (e: any) {
      console.error('Failed to fetch stats', e);
      setError('Failed to load dashboard data. Please check if the backend is running.');
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchSettings = useCallback(async () => {
    try {
      setSettings(await getSettings());
    } catch (e) {
      // Deliberately quiet: the /stats poll already reports a dead backend, and a
      // second banner saying the same thing would just push the retry button off.
      console.error('Failed to fetch settings', e);
    }
  }, []);

  const fetchHealth = useCallback(async () => {
    try {
      setHealth(await getHealth());
    } catch (e) {
      // /health failing means the backend is down, which the /stats banner
      // already says. Leave the last known value so the database banner does
      // not flicker on one dropped poll.
      console.error('Failed to fetch health', e);
    }
  }, []);

  useEffect(() => {
    const poll = () => {
      fetchStats();
      fetchSettings();
    };
    poll();
    fetchHealth();
    const interval = setInterval(poll, 5000);
    // Health changes on the timescale of someone starting a container, not of a
    // price tick, so it gets its own slower poll instead of riding the 5s one.
    const healthInterval = setInterval(fetchHealth, 30000);
    return () => {
      clearInterval(interval);
      clearInterval(healthInterval);
    };
  }, [fetchStats, fetchSettings, fetchHealth]);

  const control = async (symbol: string, action: 'start' | 'stop') => {
    try {
      await controlBot(symbol, action);
      fetchStats();
    } catch (e) {
      console.error(`Failed to ${action} ${symbol}`, e);
    }
  };

  const header = (interactive: boolean) => (
    <header className="dashboard-header">
      <div className="header-left">
        <div className="brand">
          <span className="brand-mark">NW</span>
          <div className="brand-text">
            <h1>Nadaraya-Watson Desk</h1>
            <span className="brand-sub">Kernel-regression execution terminal</span>
          </div>
        </div>
        <nav className="nav-links">
          {VIEWS.map(v => (
            <button
              key={v}
              className={`nav-btn ${view === v ? 'active' : ''}`}
              onClick={() => setView(v)}
              disabled={!interactive}
            >
              {VIEW_LABELS[v]}
            </button>
          ))}
        </nav>
      </div>
      {interactive ? (
        <label className="theme-switch" title={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}>
          <input type="checkbox" checked={theme === 'dark'} onChange={toggleTheme} />
          <span className="switch-slider"></span>
        </label>
      ) : (
        <Skeleton className="theme-toggle-skeleton" />
      )}
    </header>
  );

  // `prefs.ready` is part of the gate on purpose: rendering before the stored
  // theme and view arrive would paint the defaults and then snap to the saved
  // ones. The skeleton already existed for the /stats fetch; this reuses it.
  if (loading || !prefs.ready) {
    return (
      <div className="dashboard-container">
        {header(false)}
        <EnvelopeCurve />
        <div className="stats-grid">
          {[...Array(4)].map((_, i) => <StatCardSkeleton key={i} />)}
        </div>
        <div className="profit-section">
          <Skeleton className="section-title-skeleton" />
          <div className="profit-chips">
            {[...Array(4)].map((_, i) => <Skeleton key={i} className="profit-chip-skeleton" />)}
          </div>
        </div>
        <div className="bot-grid">
          {[...Array(3)].map((_, i) => <BotCardSkeleton key={i} />)}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="dashboard-container">
        {header(true)}
        <EnvelopeCurve />
        <div className="error-banner">
          <span>⚠️</span>
          <p>{error}</p>
          <button className="btn btn-start" onClick={() => { fetchStats(); fetchSettings(); fetchHealth(); }}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  // Every configured symbol, not just the first. The Trades page used to be
  // handed symbols[0] alone, which quietly hid every trade on any other symbol
  // the moment a second one was configured.
  const symbols = Object.keys(data?.bots || {});

  return (
    <div className="dashboard-container">
      {header(true)}
      <EnvelopeCurve />

      <DatabaseBanner health={health} />
      {prefs.ready && !prefs.available && (
        <div className="error-banner db-banner">
          <span>💾</span>
          <p>
            Your dashboard preferences are not being saved
            {prefs.error ? ` — ${prefs.error}` : '.'}
          </p>
        </div>
      )}

      {view === 'backtest' ? (
        <BacktestPage prefs={prefs} />
      ) : view === 'trades' ? (
        <TradesPage symbols={symbols} />
      ) : (
        <>
          {/* Account Overview */}
          <p className="eyebrow">Account Overview</p>
          <div className="stats-grid">
            {data?.account && Object.entries(data.account).map(([key, val]: any) => {
              // captured_at and stale describe the snapshot, not the account.
              if (key === 'time_profits' || key === 'captured_at' || key === 'stale') return null;
              const displayValue = typeof val === 'number' ? val.toLocaleString(undefined, { maximumFractionDigits: 2 }) : val;
              return (
                <div key={key} className="stat-card">
                  <div className="stat-label">{key.replace('_', ' ')}</div>
                  <div className="stat-value">{displayValue}</div>
                </div>
              );
            })}
          </div>
          {data?.account?.captured_at && (
            <p className="snapshot-note">
              Account snapshot taken{' '}
              {new Date(data.account.captured_at).toISOString().slice(0, 19).replace('T', ' ')} UTC —
              refreshed at most once a minute, because the period profits behind it are
              four year-long history scans over IPC.
            </p>
          )}

          {/* Time-based Profits */}
          <div className="profit-section">
            <h3>Profit Periods</h3>
            <div className="profit-chips">
              {data?.account?.time_profits && Object.entries(data.account.time_profits).map(([period, amount]: any) => (
                <div key={period} className="profit-chip">
                  <span>{period}:</span>
                  <b className={amount >= 0 ? 'profit-positive' : 'profit-negative'}>${amount.toFixed(2)}</b>
                </div>
              ))}
              {!data?.account?.time_profits && <span className="empty-state">No profit data available</span>}
            </div>
          </div>

          {/* Bot Control */}
          <div className="bot-grid">
            {data?.bots && Object.keys(data.bots).length > 0 ? (
              Object.entries(data.bots).map(([symbol, stats]: any) => (
                <div key={symbol} className="bot-card">
                  <div className="bot-header">
                    <h2>{symbol}</h2>
                    <span className={`status-badge ${stats.status === 'Running' ? 'status-running' : 'status-stopped'}`}>
                      <span className="live-dot" />
                      {stats.status}
                    </span>
                  </div>

                  {/* Both are reported because they can legitimately disagree --
                      the process restarted, or the thread crashed. A single word
                      is how a dead bot came to report "Running". */}
                  {stats.desired_state === 'running' && stats.status !== 'Running' && (
                    <ResumeNotice symbol={symbol} onStart={() => control(symbol, 'start')} />
                  )}
                  {stats.error && <p className="sizing-msg err">{stats.error}</p>}
                  {stats.detail && <p className="sizing-msg warn">{stats.detail}</p>}

                  {/* Indicator Stats */}
                  <div className="indicator-grid">
                    <div className="indicator-item">
                      <span className="indicator-label">Price</span>
                      <span className="indicator-value">{stats.last_close?.toFixed(2) || 'N/A'}</span>
                    </div>
                    <div className="indicator-item">
                      <span className="indicator-label">Mean</span>
                      <span className="indicator-value">{stats.out?.toFixed(2) || 'N/A'}</span>
                    </div>
                    <div className="indicator-item danger">
                      <span className="indicator-label">Upper</span>
                      <span className="indicator-value">{stats.upper?.toFixed(2) || 'N/A'}</span>
                    </div>
                    <div className="indicator-item success">
                      <span className="indicator-label">Lower</span>
                      <span className="indicator-value">{stats.lower?.toFixed(2) || 'N/A'}</span>
                    </div>
                  </div>

                  {/* Performance Stats. Wins/losses are per POSITION and decided
                      on net profit -- the old per-closing-deal count booked a
                      scale-out as its own win. */}
                  <div className="performance-grid">
                    <div className="perf-item">
                      <span className="perf-label">Total Trades</span>
                      <span className="perf-value">{stats.trades_opened || 0}</span>
                    </div>
                    <div className="perf-item success">
                      <span className="perf-label">Wins</span>
                      <span className="perf-value">{stats.wins || 0}</span>
                    </div>
                    <div className="perf-item danger">
                      <span className="perf-label">Losses</span>
                      <span className="perf-value">{stats.losses || 0}</span>
                    </div>
                    <div className={`perf-item ${stats.total_pl >= 0 ? 'success' : 'danger'}`}>
                      <span className="perf-label">Total P&L</span>
                      <span className="perf-value">${stats.total_pl?.toFixed(2) || '0.00'}</span>
                    </div>
                  </div>
                  <div className="performance-grid secondary">
                    <div className="perf-item">
                      <span className="perf-label">Win rate</span>
                      <span className="perf-value">
                        {stats.win_rate !== undefined ? `${stats.win_rate.toFixed(1)}%` : '—'}
                      </span>
                    </div>
                    <div className="perf-item">
                      <span className="perf-label" title="Trades that closed exactly flat — the designed outcome of the break-even stop">
                        Break-even
                      </span>
                      <span className="perf-value">{stats.breakeven ?? 0}</span>
                    </div>
                    <div className="perf-item">
                      <span className="perf-label" title="Trades where the scale-out fired">Scaled out</span>
                      <span className="perf-value">{stats.scaled_out ?? 0}</span>
                    </div>
                    <div className="perf-item danger">
                      <span className="perf-label" title="Deepest peak-to-trough of the closed-trade equity curve, in account currency">
                        Max DD
                      </span>
                      <span className="perf-value">${(stats.max_drawdown ?? 0).toFixed(2)}</span>
                    </div>
                  </div>
                  {stats.costs !== undefined && stats.trades_closed > 0 && (
                    <p className="perf-note">
                      Net of ${Math.abs(stats.costs).toFixed(2)} in commission and swap
                      across {stats.trades_closed} closed trade{stats.trades_closed === 1 ? '' : 's'}
                      {stats.trades_open ? ` · ${stats.trades_open} open` : ''}
                    </p>
                  )}

                  {settings[symbol] && !settings[symbol].error && (
                    <SizingEditor
                      symbol={symbol}
                      sizing={settings[symbol]}
                      onSaved={fetchSettings}
                    />
                  )}

                  <div className="button-group">
                    <button
                      onClick={() => control(symbol, 'start')}
                      disabled={stats.status === 'Running'}
                      className="btn btn-start"
                    >
                      Start
                    </button>
                    <button
                      onClick={() => control(symbol, 'stop')}
                      disabled={stats.status === 'Stopped'}
                      className="btn btn-stop"
                    >
                      Stop
                    </button>
                  </div>
                </div>
              ))
            ) : (
              <div className="empty-state-card">
                <div className="empty-icon">🤖</div>
                <h3>No bots configured</h3>
                <p>Start by configuring trading bots in your MT5 terminal</p>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
};

export default Dashboard;
