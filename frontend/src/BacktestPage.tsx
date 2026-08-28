import React, { useState } from 'react';

const API_BASE = 'http://localhost:8000';

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
        <h2>Strategy Backtester</h2>
        
        <div className="backtest-form">
          <div className="form-group">
            <label>Symbol</label>
            <input 
              value={params.symbol} 
              onChange={e => setParams({...params, symbol: e.target.value})} 
            />
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
              style={activePreset === 'week' ? { background: 'var(--accent-color)', color: 'white', borderColor: 'var(--accent-color)' } : {}}
            >
              Last Week
            </button>
            <button 
              onClick={() => handleDatePreset('month')}
              style={activePreset === 'month' ? { background: 'var(--accent-color)', color: 'white', borderColor: 'var(--accent-color)' } : {}}
            >
              Last Month
            </button>
            <button 
              onClick={() => handleDatePreset('year')}
              style={activePreset === 'year' ? { background: 'var(--accent-color)', color: 'white', borderColor: 'var(--accent-color)' } : {}}
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
          </div>
        )}
      </div>
    </div>
  );
};

export default BacktestPage;
