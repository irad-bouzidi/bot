import React, { useState, useEffect } from 'react';
import './App.css';
import BacktestPage from './BacktestPage';

const API_BASE = 'http://localhost:8000';

const Dashboard = () => {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
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
      const json = await res.json();
      setData(json);
    } catch (e) {
      console.error("Failed to fetch stats", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchStats();
    const interval = setInterval(fetchStats, 5000);
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

  if (loading) return <div className="dashboard-container">Loading Dashboard...</div>;

  return (
    <div className="dashboard-container">
      <div className="dashboard-title">
        <h1 style={{ display: 'flex', gap: '20px', alignItems: 'center' }}>
          NW Trading Dashboard
          <div className="nav-links">
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
          </div>
        </h1>
        <button className="theme-toggle" onClick={toggleTheme}>
          {theme === 'light' ? '🌙 Dark Mode' : '☀️ Light Mode'}
        </button>
      </div>
      
      {view === 'backtest' ? (
        <BacktestPage />
      ) : (
        <>
          {/* Account Overview */}
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
                  <b style={{ color: amount >= 0 ? 'var(--accent-color)' : 'var(--danger-color)' }}>${amount.toFixed(2)}</b>
                </div>
              ))}
            </div>
          </div>

          {/* Bot Control */}
          <div className="bot-grid">
            {data?.bots && Object.entries(data.bots).map(([symbol, stats]: any) => (
              <div key={symbol} className="bot-card">
                <div className="bot-header">
                  <h2>{symbol}</h2>
                  <span className={`status-badge ${stats.status === 'Running' ? 'status-running' : 'status-stopped'}`}>
                    {stats.status}
                  </span>
                </div>

                {/* Indicator Stats */}
                <div className="indicator-grid">
                  <div>Price: <b>{stats.last_close?.toFixed(2) || 'N/A'}</b></div>
                  <div>Mean: <b>{stats.out?.toFixed(2) || 'N/A'}</b></div>
                  <div>Upper: <b style={{ color: 'var(--danger-color)' }}>{stats.upper?.toFixed(2) || 'N/A'}</b></div>
                  <div>Lower: <b style={{ color: 'var(--accent-color)' }}>{stats.lower?.toFixed(2) || 'N/A'}</b></div>
                </div>

                {/* Performance Stats */}
                <div className="performance-grid">
                  <div>Total Trades: <b>{stats.trades_opened || 0}</b></div>
                  <div style={{ color: 'var(--accent-color)' }}>Wins: <b>{stats.wins || 0}</b></div>
                  <div style={{ color: 'var(--danger-color)' }}>Losses: <b>{stats.losses || 0}</b></div>
                  <div>Total P&L: <b style={{ color: stats.total_pl >= 0 ? 'var(--accent-color)' : 'var(--danger-color)' }}>${stats.total_pl?.toFixed(2) || '0.00'}</b></div>
                </div>

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
            ))}
          </div>
        </>
      )}
    </div>
  );
};

export default Dashboard;
