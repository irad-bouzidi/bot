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
  saveExitAtMean,
  saveSizing,
} from './api';
import { usePreferences } from './usePreferences';
import ConfirmDialog from './ConfirmDialog';

const Skeleton = ({ className = '' }: { className?: string }) => (
  <div className={`skeleton ${className}`} />
);

// The sign goes in front of the currency symbol, not between it and the digits:
// `$-37.42` is what a bare template literal produces and it reads as a typo.
const money = (n: number | null | undefined) =>
  n === null || n === undefined ? '—' : `${n < 0 ? '-' : ''}$${Math.abs(n).toFixed(2)}`;

const utcStamp = (iso: string) =>
  new Date(iso).toISOString().slice(0, 19).replace('T', ' ');

// The account panel's age comes from the API (`now() - MAX(captured_at)`,
// measured by Postgres) rather than from subtracting `captured_at` against this
// browser's clock, so a laptop with the wrong time cannot invent or hide
// staleness. Formatted coarsely on purpose: the question this answers is
// "minutes or hours?", not "how many seconds?".
const snapshotAge = (seconds: number) => {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
};

type View = 'dashboard' | 'backtest' | 'trades';
const VIEWS: View[] = ['dashboard', 'backtest', 'trades'];
const VIEW_LABELS: Record<View, string> = {
  dashboard: 'Dashboard',
  backtest: 'Backtest',
  trades: 'Trades',
};

// Signature device: a stylized Nadaraya-Watson kernel-regression envelope —
// the exact smoothed mean + upper/lower bands these bots trade against. Drawn
// full-bleed under the header rather than inside the content column, so it
// reads as a rule under the nav and does not set the page's top margin.
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

const StatSkeleton = () => (
  <div className="stat">
    <Skeleton className="sk-line md" />
    <Skeleton className="sk-value" />
  </div>
);

const BotCardSkeleton = () => (
  <div className="card bot-card">
    <div className="card-header">
      <Skeleton className="sk-title" />
      <Skeleton className="sk-badge" />
    </div>
    <div className="card-content">
      <div className="metric-grid">
        <Skeleton className="sk-metric" />
        <Skeleton className="sk-metric" />
        <Skeleton className="sk-metric" />
        <Skeleton className="sk-metric" />
      </div>
      <div className="metric-grid">
        <Skeleton className="sk-metric" />
        <Skeleton className="sk-metric" />
        <Skeleton className="sk-metric" />
        <Skeleton className="sk-metric" />
      </div>
      <div className="panel">
        <Skeleton className="sk-line lg" />
        <div className="form-grid two">
          <Skeleton className="sk-field" />
          <Skeleton className="sk-field" />
        </div>
      </div>
    </div>
    <div className="card-footer split">
      <Skeleton className="sk-btn" />
      <Skeleton className="sk-btn" />
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
    <div className="alert alert-destructive">
      <span className="alert-icon">🗄️</span>
      <div className="alert-body">
        <p>
          {!reachable
            ? `Postgres at ${url} is not reachable. Nothing is being persisted — trade history, sizing changes and preferences are all being dropped.`
            : `Postgres is reachable but the schema is incomplete (version ${schema_version}).`}
        </p>
        <code className="alert-cmd">{migrate_command}</code>
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
  <div className="alert alert-warning">
    <span className="alert-icon">⏸</span>
    <div className="alert-body">
      <p>
        <b>{symbol}</b> was running before the last shutdown and was not
        auto-started.
      </p>
    </div>
    <button className="btn btn-outline btn-sm" onClick={onStart}>
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
    <div>
      <button className="link-btn" onClick={toggle}>
        {open ? 'Hide' : 'Show'} sizing history
      </button>
      {open && (
        <>
          {error && <p className="msg err">{error}</p>}
          {rows && !rows.length && <p className="hint muted">No changes recorded.</p>}
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
                    {/* Without this, a flag-only edit renders identically to the
                        row above it and reads as "nothing changed". */}
                    {r.prev_exit_at_mean !== null &&
                      r.prev_exit_at_mean !== r.exit_at_mean && (
                        <>
                          {' · '}
                          centre-line exit {r.prev_exit_at_mean ? 'on' : 'off'} →{' '}
                          {r.exit_at_mean ? 'on' : 'off'}
                        </>
                      )}
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
    <>
      <div className="panel">
        <div className="panel-head">
          <span className="panel-title">Position sizing</span>
          <span className={`panel-meta ${lotOk ? '' : 'muted'}`}>
            {lotOk ? `~$${(sizing.risk_per_lot * lotNum).toFixed(0)} at risk / trade` : '—'}
          </span>
        </div>

        <div className="form-grid two">
          <label className="field">
            <span>Lot size</span>
            <input
              className="input"
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
          <label className="field">
            <span>Scale-out lots</span>
            <input
              className="input"
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

        <div className="stack">
          <p className="hint">
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
          <p className="hint muted">
            Broker min {sizing.volume_min} · step {sizing.volume_step}
            {sizing.broker_limits ? '' : ' (terminal offline — defaults shown)'}
            {sizing.splittable === false && outNum > 0
              ? ` · ${lot} lots cannot be split here, so only the stop will move.`
              : ''}
          </p>

          {problem && <p className="msg err">{problem}</p>}
          {msg && <p className={`msg ${msg.ok ? 'ok' : 'err'}`}>{msg.text}</p>}
        </div>

        <div className="form-actions">
          <button className="btn btn-primary btn-sm" onClick={save} disabled={!dirty || busy || !!problem}>
            {busy ? 'Saving…' : 'Save sizing'}
          </button>
          <button className="btn btn-outline btn-sm" onClick={reset} disabled={!dirty || busy}>
            Reset
          </button>
        </div>
      </div>

      <ExitRuleToggle symbol={symbol} sizing={sizing} onSaved={onSaved} />
      <SizingHistory symbol={symbol} />
    </>
  );
};

// Deliberately NOT a field in the sizing form above, for three reasons: it is an
// exit rule and not a size; every input up there is disabled while a position is
// open and this one must not be; and Save is gated on the sizing form being
// valid, which would make this unreachable in exactly the state it matters in.
// It applies immediately for the same reason -- there is nothing to validate
// against, so a Save button would only add a step.
const ExitRuleToggle = ({ symbol, sizing, onSaved }: { symbol: string; sizing: any; onSaved: () => void }) => {
  const [on, setOn] = useState<boolean>(!!sizing.exit_at_mean);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // Adopt the server's value whenever it changes and we are not mid-request.
  // `sizing.exit_at_mean` must be in the dependency list or a change made in
  // another tab would never appear here.
  useEffect(() => {
    if (busy) return;
    setOn(!!sizing.exit_at_mean);
  }, [sizing.exit_at_mean, busy]);

  const toggle = async (next: boolean) => {
    setBusy(true);
    setMsg(null);
    // Optimistic, then corrected from the response: the switch has to feel
    // immediate, but what it reports must be what the database accepted.
    setOn(next);
    try {
      const data = await saveExitAtMean(symbol, next);
      setOn(!!data.exit_at_mean);
      setMsg({ ok: true, text: data.notes?.length ? data.notes.join(' ') : 'Saved.' });
      onSaved();
    } catch (e: any) {
      // Snapped back rather than left showing what was asked for. A switch that
      // displays "off" after a refused write is the one failure this control
      // cannot have: the user would leave a trade running believing the rule
      // was off, and the bot would close it at the centre line anyway.
      setOn(!!sizing.exit_at_mean);
      setMsg({ ok: false, text: e instanceof ApiError ? e.message : 'Could not reach the backend.' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="panel-title">Exit rules</span>
      </div>
      <label className="checkbox-row">
        <input
          className="checkbox"
          type="checkbox"
          checked={on}
          disabled={busy}
          onChange={e => toggle(e.target.checked)}
        />
        <span>Close at the centre line</span>
      </label>
      <div className="stack">
        <p className="hint">
          {on ? (
            <>
              <b>On.</b> A position also closes as soon as a closed bar prints back at the
              envelope's centre line — <b>before</b> the target. The centre sits between the
              scale-out trigger and the target, so a scaled-out runner is usually closed here
              instead of reaching its take-profit.
            </>
          ) : (
            <>
              <b>Off.</b> The only exits are the stop, the target, and the break-even stop
              after a scale-out. Nothing else closes a position.
            </>
          )}
        </p>
        {sizing.locked && (
          <p className="hint muted">
            Changeable while a position is open, unlike the lot numbers above — it derives
            nothing from the position. Takes effect on the next closed bar.
          </p>
        )}
        {msg && <p className={`msg ${msg.ok ? 'ok' : 'err'}`}>{msg.text}</p>}
      </div>
    </div>
  );
};

const Dashboard = () => {
  const [data, setData] = useState<any>(null);
  const [settings, setSettings] = useState<any>({});
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // The Start/Stop press, held until it is confirmed. Start places real orders
  // and Stop can strand an open position, so neither is a single click any
  // more -- see `requestControl` for which of the two need confirming.
  const [pending, setPending] = useState<{ symbol: string; action: 'start' | 'stop' } | null>(null);
  const [controlBusy, setControlBusy] = useState(false);
  // Per symbol, because a failed Start on gold says nothing about Bitcoin. It
  // used to go to console.error only: the button did nothing visible and the
  // card went on saying "Stopped" with no reason given.
  const [controlErrors, setControlErrors] = useState<Record<string, string>>({});

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
      setSettingsError(null);
    } catch (e: any) {
      // Still not a banner -- the /stats poll already reports a dead backend and
      // a second one would push its retry button off the screen. But no longer
      // silent either: `settings` keeps its last value on failure, so the panel
      // goes on showing the values from the last good poll. A stale lot size is
      // visible against the dollar-risk figure printed beside it; a stale
      // BOOLEAN is not -- a switch reading "off" while the bot is running with
      // the rule on is indistinguishable from the truth.
      console.error('Failed to fetch settings', e);
      setSettingsError(e?.message || 'Could not reach the backend.');
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
    setControlBusy(true);
    try {
      await controlBot(symbol, action);
      setControlErrors(prev => {
        const next = { ...prev };
        delete next[symbol];
        return next;
      });
      setPending(null);
      fetchStats();
    } catch (e: any) {
      // Reported on the card rather than only to the console. /control answers
      // 200 with an { error } body when it refuses -- a missing terminal, a
      // symbol the broker will not select -- and api.ts turns that into a
      // throw, so a refusal and a dead backend both land here with words.
      setPending(null);
      setControlErrors(prev => ({
        ...prev,
        [symbol]: e instanceof ApiError ? e.message : `Could not ${action} ${symbol}.`,
      }));
    } finally {
      setControlBusy(false);
    }
  };

  // Start always confirms: it is the one control on this dashboard that begins
  // placing real orders, and it has no authentication in front of it.
  //
  // Stop confirms only while a position is open. Stopping an idle bot is
  // reversible with the button next to it, but stopping one that holds a trade
  // leaves that trade with nothing to fire its scale-out or pull its stop to
  // break-even -- and that consequence is invisible from the button.
  const requestControl = (symbol: string, action: 'start' | 'stop') => {
    const openPositions = settings[symbol]?.open_positions || 0;
    if (action === 'stop' && !openPositions) {
      control(symbol, 'stop');
      return;
    }
    setPending({ symbol, action });
  };

  // The header is the same object in all three states (loading, error, loaded)
  // and on all three views, so it is built once. `interactive` is the only
  // difference: nothing on it can be pressed before the stored view arrives.
  const header = (interactive: boolean) => (
    <>
      <header className="app-header">
        <div className="container header-inner">
          <div className="header-left">
            <div className="brand">
              <span className="brand-mark">NW</span>
              <div className="brand-text">
                <h1>Nadaraya-Watson Desk</h1>
                <span className="brand-sub">Kernel-regression execution terminal</span>
              </div>
            </div>
            <nav className="toggle-group">
              {VIEWS.map(v => (
                <button
                  key={v}
                  className={`toggle ${view === v ? 'active' : ''}`}
                  onClick={() => setView(v)}
                  disabled={!interactive}
                  aria-pressed={view === v}
                >
                  {VIEW_LABELS[v]}
                </button>
              ))}
            </nav>
          </div>
          {interactive ? (
            <label
              className="switch"
              title={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
            >
              <input
                type="checkbox"
                checked={theme === 'dark'}
                onChange={toggleTheme}
                aria-label="Dark mode"
              />
              <span className="switch-track">
                <span className="switch-thumb" />
              </span>
            </label>
          ) : (
            <Skeleton className="sk-switch" />
          )}
        </div>
      </header>
      <EnvelopeCurve />
    </>
  );

  // `prefs.ready` is part of the gate on purpose: rendering before the stored
  // theme and view arrive would paint the defaults and then snap to the saved
  // ones. The skeleton already existed for the /stats fetch; this reuses it.
  if (loading || !prefs.ready) {
    return (
      <div className="app-shell">
        {header(false)}
        <main className="container page">
          <div className="card">
            <div className="card-header">
              <Skeleton className="sk-title" />
            </div>
            <div className="card-content">
              <div className="stat-grid">
                {[...Array(4)].map((_, i) => <StatSkeleton key={i} />)}
              </div>
            </div>
          </div>
          <div className="bot-grid">
            {[...Array(2)].map((_, i) => <BotCardSkeleton key={i} />)}
          </div>
        </main>
      </div>
    );
  }

  if (error) {
    return (
      <div className="app-shell">
        {header(true)}
        <main className="container page">
          <div className="alert alert-destructive">
            <span className="alert-icon">⚠️</span>
            <div className="alert-body">
              <p>{error}</p>
            </div>
            <button
              className="btn btn-outline btn-sm"
              onClick={() => { fetchStats(); fetchSettings(); fetchHealth(); }}
            >
              Retry
            </button>
          </div>
        </main>
      </div>
    );
  }

  // Every configured symbol, not just the first. The Trades page used to be
  // handed symbols[0] alone, which quietly hid every trade on any other symbol
  // the moment a second one was configured.
  const symbols = Object.keys(data?.bots || {});

  return (
    <div className="app-shell">
      {header(true)}
      <main className="container page">
        <DatabaseBanner health={health} />
        {prefs.ready && !prefs.available && (
          <div className="alert alert-destructive">
            <span className="alert-icon">💾</span>
            <div className="alert-body">
              <p>
                Your dashboard preferences are not being saved
                {prefs.error ? ` — ${prefs.error}` : '.'}
              </p>
            </div>
          </div>
        )}

        {view === 'backtest' ? (
          <BacktestPage prefs={prefs} />
        ) : view === 'trades' ? (
          <TradesPage symbols={symbols} />
        ) : (
          <>
            {/* Account overview. One card, two blocks: the readings, then the
                period profits — they come from the same throttled snapshot, so
                separating them into two cards would imply two refresh rates. */}
            <section className="card">
              <div className="card-header">
                <div>
                  <p className="eyebrow">Account</p>
                  <h2 className="card-title">Account overview</h2>
                  <p className="card-desc">
                    Read from the MT5 terminal and stored as a snapshot, so the equity
                    curve survives a restart.
                  </p>
                </div>
              </div>
              <div className="card-content">
                <div className="stat-grid">
                  {data?.account && Object.entries(data.account).map(([key, val]: any) => {
                    // captured_at, age_seconds and stale describe the snapshot, not
                    // the account, and are reported in the note under the grid.
                    if (key === 'time_profits' || key === 'captured_at'
                        || key === 'age_seconds' || key === 'stale') return null;
                    const displayValue = typeof val === 'number' ? val.toLocaleString(undefined, { maximumFractionDigits: 2 }) : val;
                    return (
                      <div key={key} className="stat">
                        <span className="stat-label">{key.replace('_', ' ')}</span>
                        <span className="stat-value">{displayValue}</span>
                      </div>
                    );
                  })}
                </div>

                {data?.account?.captured_at && (
                  data.account.stale ? (
                    // Past the throttle with no fresh capture means MT5 did not
                    // answer, so these numbers are frozen. The balance is still worth
                    // showing -- it is the last one that was true -- but the note that
                    // used to sit here promised a once-a-minute refresh, which turned
                    // a dead terminal into what looked like a clock bug.
                    <p className="note warn">
                      ⚠ Not refreshing. This reading is{' '}
                      {snapshotAge(data.account.age_seconds || 0)} old, taken{' '}
                      {utcStamp(data.account.captured_at)} UTC — the MT5 terminal is not
                      answering, so the balance, equity and profit periods above are the
                      last ones that were true, not the current ones. The bot cards below
                      are read from Postgres and are unaffected.
                    </p>
                  ) : (
                    <p className="note">
                      Account snapshot taken {utcStamp(data.account.captured_at)} UTC —
                      refreshed at most once a minute, because the period profits behind it are
                      four year-long history scans over IPC.
                    </p>
                  )
                )}

                <hr className="separator" />

                <div className="stack">
                  <div className="panel-head">
                    <span className="panel-title">Profit periods</span>
                  </div>
                  {data?.account?.time_profits ? (
                    <div className="metric-grid">
                      {Object.entries(data.account.time_profits).map(([period, amount]: any) => (
                        <div
                          key={period}
                          className={`metric ${amount >= 0 ? 'success' : 'danger'}`}
                        >
                          <span className="metric-label">{period}</span>
                          <span className="metric-value">{money(amount)}</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="hint muted">No profit data available.</p>
                  )}
                </div>
              </div>
            </section>

            {/* Bot control */}
            <div className="bot-grid">
              {data?.bots && Object.keys(data.bots).length > 0 ? (
                Object.entries(data.bots).map(([symbol, stats]: any) => (
                  <section key={symbol} className="card bot-card">
                    <div className="card-header">
                      <div>
                        <h2 className="card-title mono">{symbol}</h2>
                        <p className="card-desc">
                          {settings[symbol] && !settings[symbol].error
                            ? `Stop ${settings[symbol].sl_pips} pips · target ${settings[symbol].tp_pips} pips`
                            : 'Live envelope reading and controls'}
                        </p>
                      </div>
                      <span
                        className={`badge ${stats.status === 'Running' ? 'badge-success' : 'badge-outline'}`}
                      >
                        <span className="status-dot" />
                        {stats.status}
                      </span>
                    </div>

                    <div className="card-content">
                      {/* Both are reported because they can legitimately disagree --
                          the process restarted, or the thread crashed. A single word
                          is how a dead bot came to report "Running". */}
                      {stats.desired_state === 'running' && stats.status !== 'Running' && (
                        <ResumeNotice
                          symbol={symbol}
                          onStart={() => requestControl(symbol, 'start')}
                        />
                      )}
                      {controlErrors[symbol] && (
                        <p className="msg err">{controlErrors[symbol]}</p>
                      )}
                      {stats.error && <p className="msg err">{stats.error}</p>}
                      {stats.detail && <p className="msg warn">{stats.detail}</p>}

                      {/* Indicator stats */}
                      <div className="stack">
                        <div className="panel-head">
                          <span className="panel-title">Envelope</span>
                        </div>
                        <div className="metric-grid">
                          <div className="metric">
                            <span className="metric-label">Price</span>
                            <span className="metric-value">{stats.last_close?.toFixed(2) || 'N/A'}</span>
                          </div>
                          <div className="metric">
                            <span className="metric-label">Mean</span>
                            <span className="metric-value">{stats.out?.toFixed(2) || 'N/A'}</span>
                          </div>
                          <div className="metric danger">
                            <span className="metric-label">Upper</span>
                            <span className="metric-value">{stats.upper?.toFixed(2) || 'N/A'}</span>
                          </div>
                          <div className="metric success">
                            <span className="metric-label">Lower</span>
                            <span className="metric-value">{stats.lower?.toFixed(2) || 'N/A'}</span>
                          </div>
                        </div>
                      </div>

                      {/* Performance stats. Wins/losses are per POSITION and decided
                          on net profit -- the old per-closing-deal count booked a
                          scale-out as its own win. */}
                      <div className="stack">
                        <div className="panel-head">
                          <span className="panel-title">Performance</span>
                        </div>
                        <div className="metric-grid">
                          <div className="metric">
                            <span className="metric-label">Trades</span>
                            <span className="metric-value">{stats.trades_opened || 0}</span>
                          </div>
                          <div className="metric success">
                            <span className="metric-label">Wins</span>
                            <span className="metric-value">{stats.wins || 0}</span>
                          </div>
                          <div className="metric danger">
                            <span className="metric-label">Losses</span>
                            <span className="metric-value">{stats.losses || 0}</span>
                          </div>
                          <div className={`metric ${stats.total_pl >= 0 ? 'success' : 'danger'}`}>
                            <span className="metric-label">Total P&L</span>
                            <span className="metric-value">{money(stats.total_pl ?? 0)}</span>
                          </div>
                          <div className="metric">
                            <span className="metric-label">Win rate</span>
                            <span className="metric-value">
                              {stats.win_rate !== undefined ? `${stats.win_rate.toFixed(1)}%` : '—'}
                            </span>
                          </div>
                          <div className="metric">
                            <span
                              className="metric-label"
                              title="Trades that closed exactly flat — the designed outcome of the break-even stop"
                            >
                              Break-even
                            </span>
                            <span className="metric-value">{stats.breakeven ?? 0}</span>
                          </div>
                          <div className="metric">
                            <span className="metric-label" title="Trades where the scale-out fired">
                              Scaled out
                            </span>
                            <span className="metric-value">{stats.scaled_out ?? 0}</span>
                          </div>
                          <div className="metric danger">
                            <span
                              className="metric-label"
                              title="Deepest peak-to-trough of the closed-trade equity curve, in account currency"
                            >
                              Max DD
                            </span>
                            <span className="metric-value">${(stats.max_drawdown ?? 0).toFixed(2)}</span>
                          </div>
                        </div>
                        {stats.costs !== undefined && stats.trades_closed > 0 && (
                          <p className="note">
                            Net of ${Math.abs(stats.costs).toFixed(2)} in commission and swap
                            across {stats.trades_closed} closed trade{stats.trades_closed === 1 ? '' : 's'}
                            {stats.trades_open ? ` · ${stats.trades_open} open` : ''}
                          </p>
                        )}
                      </div>

                      {settingsError && (
                        <p className="msg err">
                          Sizing and exit rules could not be refreshed — {settingsError}
                          {settings[symbol] ? ' The values below are from the last good read.' : ''}
                        </p>
                      )}
                      {settings[symbol] && !settings[symbol].error && (
                        <SizingEditor
                          symbol={symbol}
                          sizing={settings[symbol]}
                          onSaved={fetchSettings}
                        />
                      )}
                    </div>

                    <div className="card-footer split">
                      <button
                        onClick={() => requestControl(symbol, 'start')}
                        disabled={stats.status === 'Running' || controlBusy}
                        className="btn btn-primary"
                      >
                        Start
                      </button>
                      <button
                        onClick={() => requestControl(symbol, 'stop')}
                        disabled={stats.status === 'Stopped' || controlBusy}
                        className="btn btn-outline-destructive"
                      >
                        Stop
                      </button>
                    </div>
                  </section>
                ))
              ) : (
                <div className="card">
                  <div className="empty-state">
                    <div className="empty-icon">🤖</div>
                    <h3>No bots configured</h3>
                    <p>Start by configuring trading bots in your MT5 terminal.</p>
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </main>

      {/* One instance for the whole grid: the pending press names its own
          symbol, so a dialog per card would be N copies of the same thing with
          only one of them ever open. */}
      <ConfirmDialog
        open={!!pending}
        title={
          pending?.action === 'start'
            ? `Start live trading on ${pending.symbol}?`
            : `Stop ${pending?.symbol} with a position open?`
        }
        description={
          pending?.action === 'start' ? (
            <>
              <p>
                This places <b>real orders</b> with real money on the next closed bar
                that meets the envelope rule.
              </p>
              <p>
                {settings[pending.symbol] && !settings[pending.symbol].error ? (
                  <>
                    At the stored size of {settings[pending.symbol].lot_size} lots, each
                    trade risks about{' '}
                    <b>
                      ~$
                      {(
                        settings[pending.symbol].risk_per_lot *
                        settings[pending.symbol].lot_size
                      ).toFixed(0)}
                    </b>{' '}
                    to its stop. There is no daily loss cap and no margin check.
                  </>
                ) : (
                  <>
                    The size this will trade with could not be read, so the risk per
                    trade cannot be shown here.
                  </>
                )}
              </p>
            </>
          ) : (
            <>
              <p>
                Stopping does <b>not</b> close the{' '}
                {pending ? settings[pending.symbol]?.open_positions || 0 : 0} open
                position.
              </p>
              <p>
                The broker-side stop and target stay where they are, but nothing will
                fire the scale-out or pull the stop to break-even while the bot is
                stopped.
              </p>
            </>
          )
        }
        confirmLabel={pending?.action === 'start' ? 'Start trading' : 'Stop anyway'}
        tone={pending?.action === 'start' ? 'default' : 'destructive'}
        busy={controlBusy}
        onConfirm={() => pending && control(pending.symbol, pending.action)}
        onCancel={() => setPending(null)}
      />
    </div>
  );
};

export default Dashboard;
