import React, { useState, useEffect } from 'react';
import './App.css';
import BacktestPage from './BacktestPage';

const API_BASE = 'http://localhost:8000';

const Skeleton = ({ className = '' }: { className?: string }) => (
  <div className={`skeleton ${className}`} />
);

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
      const res = await fetch(`${API_BASE}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, lot_size: lotNum, scale_out_lots: outNum }),
      });
      const data = await res.json();
      if (data.error) {
        setMsg({ ok: false, text: data.error });
        return;
      }
      // Show what was actually applied, not what was typed: the backend snaps to
      // the broker's volume step, so 0.155 comes back as 0.16.
      setLot(String(data.lot_size));
      setScaleOut(String(data.scale_out_lots));
      setDirty(false);
      setMsg({ ok: true, text: data.notes?.length ? data.notes.join(' ') : 'Saved.' });
      onSaved();
    } catch (e) {
      setMsg({ ok: false, text: 'Could not reach the backend.' });
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
    </div>
  );
};

const Dashboard = () => {
  const [data, setData] = useState<any>(null);
  const [settings, setSettings] = useState<any>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useState<'light' | 'dark'>('light');
  const [view, setView] = useState<'dashboard' | 'backtest'>('dashboard');

  useEffect(() => {
    const savedTheme = localStorage.getItem('theme') as 'light' | 'dark';
    if (savedTheme) {
      setTheme(savedTheme);
      document.documentElement.setAttribute('data-theme', savedTheme);
    }
  }, []);

  const toggleTheme = () => {
    const newTheme = theme === 'light' ? 'dark' : 'light';
    setTheme(newTheme);
    document.documentElement.setAttribute('data-theme', newTheme);
    localStorage.setItem('theme', newTheme);
  };

  const fetchStats = async () => {
    try {
      const res = await fetch(`${API_BASE}/stats`);
      if (!res.ok) throw new Error('Failed to fetch stats');
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (e) {
      console.error("Failed to fetch stats", e);
      setError('Failed to load dashboard data. Please check if the backend is running.');
    } finally {
      setLoading(false);
    }
  };

  const fetchSettings = async () => {
    try {
      const res = await fetch(`${API_BASE}/settings`);
      if (!res.ok) return;
      setSettings(await res.json());
    } catch (e) {
      // Deliberately quiet: the /stats poll already reports a dead backend, and a
      // second banner saying the same thing would just push the retry button off.
      console.error('Failed to fetch settings', e);
    }
  };

  useEffect(() => {
    const poll = () => {
      fetchStats();
      fetchSettings();
    };
    poll();
    const interval = setInterval(poll, 5000);
    return () => clearInterval(interval);
  }, []);

  const controlBot = async (symbol: string, action: 'start' | 'stop') => {
    try {
      await fetch(`${API_BASE}/control`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, action }),
      });
      fetchStats();
    } catch (e) {
      console.error(`Failed to ${action} ${symbol}`, e);
    }
  };

  if (loading) {
    return (
      <div className="dashboard-container">
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
              <button className="nav-btn active" disabled>Dashboard</button>
              <button className="nav-btn" disabled>Backtest</button>
            </nav>
          </div>
          <Skeleton className="theme-toggle-skeleton" />
        </header>
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
              <button className={`nav-btn ${view === 'dashboard' ? 'active' : ''}`} onClick={() => setView('dashboard')}>Dashboard</button>
              <button className={`nav-btn ${view === 'backtest' ? 'active' : ''}`} onClick={() => setView('backtest')}>Backtest</button>
            </nav>
          </div>
          <label className="theme-switch" title={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}>
            <input
              type="checkbox"
              checked={theme === 'dark'}
              onChange={toggleTheme}
            />
            <span className="switch-slider"></span>
          </label>
        </header>
        <EnvelopeCurve />
        <div className="error-banner">
          <span>⚠️</span>
          <p>{error}</p>
          <button className="btn btn-start" onClick={() => { fetchStats(); fetchSettings(); }}>Retry</button>
        </div>
      </div>
    );
  }

  return (
    <div className="dashboard-container">
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
            <button 
              className={`nav-btn ${view === 'dashboard' ? 'active' : ''}`} 
              onClick={() => setView('dashboard')}
            >
              Dashboard
            </button>
            <button 
              className={`nav-btn ${view === 'backtest' ? 'active' : ''}`} 
              onClick={() => setView('backtest')}
            >
              Backtest
            </button>
          </nav>
        </div>
        <label className="theme-switch" title={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}>
          <input 
            type="checkbox" 
            checked={theme === 'dark'} 
            onChange={toggleTheme} 
          />
          <span className="switch-slider"></span>
        </label>
      </header>
      <EnvelopeCurve />

      {view === 'backtest' ? (
        <BacktestPage />
      ) : (
        <>
          {/* Account Overview */}
          <p className="eyebrow">Account Overview</p>
          <div className="stats-grid">
            {data?.account && Object.entries(data.account).map(([key, val]: any) => {
              if (key === 'time_profits') return null;
              const displayValue = typeof val === 'number' ? val.toLocaleString(undefined, { maximumFractionDigits: 2 }) : val;
              return (
                <div key={key} className="stat-card">
                  <div className="stat-label">{key.replace('_', ' ')}</div>
                  <div className="stat-value">{displayValue}</div>
                </div>
              );
            })}
          </div>

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

                  {/* Performance Stats */}
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

                  {settings[symbol] && !settings[symbol].error && (
                    <SizingEditor
                      symbol={symbol}
                      sizing={settings[symbol]}
                      onSaved={fetchSettings}
                    />
                  )}

                  <div className="button-group">
                    <button 
                      onClick={() => controlBot(symbol, 'start')}
                      disabled={stats.status === 'Running'}
                      className="btn btn-start"
                    >
                      Start
                    </button>
                    <button 
                      onClick={() => controlBot(symbol, 'stop')}
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
