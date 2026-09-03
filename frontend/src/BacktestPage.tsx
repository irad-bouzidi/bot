import React, { useEffect, useState } from 'react';

const API_BASE = 'http://localhost:8000';

const SUPPORTED_SYMBOLS = ['XAUUSDm'];

const BacktestPage = () => {
  const [params, setParams] = useState({
    symbol: 'XAUUSDm',
    start_date: '',
    end_date: new Date().toISOString().split('T')[0],
    initial_balance: 1000,
  });
  const [activePreset, setActivePreset] = useState<'week' | 'month' | 'year' | null>(null);
  const [result, setResult] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Sizing is seeded from what the bot is actually configured with, so an
  // untouched form backtests the bot as it stands rather than a hardcoded 0.1.
  const [sizing, setSizing] = useState<any>(null);
  const [lot, setLot] = useState('0.1');
  const [scaleOut, setScaleOut] = useState('0.05');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/settings`);
        if (!res.ok) return;
        const all = await res.json();
        const live = all[params.symbol];
        if (cancelled || !live || live.error) return;
        setSizing(live);
        setLot(String(live.lot_size));
        setScaleOut(String(live.scale_out_lots));
      } catch (e) {
        // Leave the defaults in place; the run itself will report a dead backend.
      }
    })();
    return () => { cancelled = true; };
  }, [params.symbol]);

  const lotNum = parseFloat(lot);
  const outNum = parseFloat(scaleOut);
  const lotOk = isFinite(lotNum) && lotNum > 0;
  const outOk = isFinite(outNum) && outNum >= 0 && (!lotOk || outNum < lotNum);
  const share = lotOk && outOk && outNum > 0 ? (outNum / lotNum) * 100 : 0;
  const step = sizing?.volume_step ?? 0.01;
  const triggerPrice = sizing ? sizing.be_trigger_pips * sizing.pip : 5;

  const formatDate = (date: Date) => {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  };

  const handleDatePreset = (period: 'week' | 'month' | 'year') => {
    const end = new Date();
    const start = new Date();
    
    if (period === 'week') start.setDate(end.getDate() - 7);
    else if (period === 'month') start.setMonth(end.getMonth() - 1);
    else if (period === 'year') start.setFullYear(end.getFullYear() - 1);

    setActivePreset(period);
    setParams({
      ...params,
      start_date: formatDate(start),
      end_date: formatDate(end),
    });
  };

  const runBacktest = async () => {
    if (!params.start_date || !params.end_date) {
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
      const res = await fetch(`${API_BASE}/backtest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...params,
          start_date: `${params.start_date}T00:00:00`,
          end_date: `${params.end_date}T23:59:59`,
          lot_size: lotNum,
          scale_out_lots: outNum,
        }),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      setResult(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
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
            <select 
              value={params.symbol} 
              onChange={e => setParams({...params, symbol: e.target.value})} 
            >
              {SUPPORTED_SYMBOLS.map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          <div className="form-group">
            <label>Initial Balance ($)</label>
            <input 
              type="number" 
              value={params.initial_balance} 
              onChange={e => setParams({...params, initial_balance: parseFloat(e.target.value)})} 
            />
          </div>

          <div className="form-group">
            <label>Lot size</label>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              step={step}
              value={lot}
              onChange={e => setLot(e.target.value)}
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
              value={scaleOut}
              onChange={e => setScaleOut(e.target.value)}
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
              value={params.start_date} 
              onChange={e => {
                setParams({...params, start_date: e.target.value});
                setActivePreset(null);
              }} 
            />
          </div>

          <div className="form-group">
            <label>End Date</label>
            <input 
              type="date" 
              value={params.end_date} 
              onChange={e => {
                setParams({...params, end_date: e.target.value});
                setActivePreset(null);
              }} 
            />
          </div>

          <div className="preset-group">
             <button 
               onClick={() => handleDatePreset('week')}
               className={`preset-btn ${activePreset === 'week' ? 'active' : ''}`}
             >
               Last Week
             </button>
             <button 
               onClick={() => handleDatePreset('month')}
               className={`preset-btn ${activePreset === 'month' ? 'active' : ''}`}
             >
               Last Month
             </button>
             <button 
               onClick={() => handleDatePreset('year')}
               className={`preset-btn ${activePreset === 'year' ? 'active' : ''}`}
             >
               Last Year
             </button>
          </div>

          <button className="btn-run" onClick={runBacktest} disabled={loading}>
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
      </div>
    </div>
  );
};

export default BacktestPage;
