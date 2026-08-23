import React, { useState, useEffect } from 'react';

const API_BASE = 'http://localhost:8000';

const Dashboard = () => {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

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

  if (loading) return <div style={{ padding: '20px', fontFamily: 'sans-serif' }}>Loading Dashboard...</div>;

  return (
    <div style={{ padding: '20px', fontFamily: 'sans-serif', backgroundColor: '#f4f7f6', minHeight: '100vh' }}>
      <h1 style={{ color: '#333', marginBottom: '30px' }}>NW Trading Dashboard</h1>
      
      {/* Account Overview */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '20px', marginBottom: '30px' }}>
        {data?.account && Object.entries(data.account).map(([key, val]: any) => {
          if (key === 'time_profits') return null;
          const displayValue = typeof val === 'number' ? val.toLocaleString(undefined, { maximumFractionDigits: 2 }) : val;
          return (
            <div key={key} style={{ background: '#fff', padding: '15px', borderRadius: '8px', boxShadow: '0 2px 4px rgba(0,0,0,0.1)' }}>
              <div style={{ fontSize: '12px', color: '#666', textTransform: 'uppercase' }}>{key.replace('_', ' ')}</div>
              <div style={{ fontSize: '20px', fontWeight: 'bold', color: '#222' }}>{displayValue}</div>
            </div>
          );
        })}
      </div>

      {/* Time-based Profits */}
      <div style={{ marginBottom: '30px' }}>
        <h3 style={{ color: '#555', marginBottom: '15px' }}>Profit Periods</h3>
        <div style={{ display: 'flex', gap: '15px', flexWrap: 'wrap' }}>
          {data?.account?.time_profits && Object.entries(data.account.time_profits).map(([period, amount]: any) => (
            <div key={period} style={{ background: '#fff', padding: '10px 20px', borderRadius: '20px', border: '1px solid #ddd', fontSize: '14px' }}>
              <span style={{ textTransform: 'capitalize', color: '#666', marginRight: '10px' }}>{period}:</span>
              <b style={{ color: amount >= 0 ? '#48bb78' : '#f56565' }}>${amount.toFixed(2)}</b>
            </div>
          ))}
        </div>
      </div>

      {/* Bot Control */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(350px, 1fr))', gap: '20px' }}>
        {data?.bots && Object.entries(data.bots).map(([symbol, stats]: any) => (
          <div key={symbol} style={{ background: '#fff', padding: '20px', borderRadius: '12px', boxShadow: '0 4px 6px rgba(0,0,0,0.1)', border: '1px solid #ddd' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
              <h2 style={{ margin: 0 }}>{symbol}</h2>
              <span style={{ 
                padding: '4px 8px', 
                borderRadius: '4px', 
                fontSize: '12px', 
                backgroundColor: stats.status === 'Running' ? '#e6fffa' : '#fff5f5',
                color: stats.status === 'Running' ? '#2c7a7b' : '#c53030',
                fontWeight: 'bold',
                border: `1px solid ${stats.status === 'Running' ? '#81e6d9' : '#feb2b2'}`
              }}>
                {stats.status}
              </span>
            </div>

            {/* Indicator Stats */}
            <div style={{ marginBottom: '20px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', fontSize: '14px', padding: '10px', backgroundColor: '#f9f9f9', borderRadius: '8px' }}>
              <div>Price: <b>{stats.last_close?.toFixed(2) || 'N/A'}</b></div>
              <div>Mean: <b>{stats.out?.toFixed(2) || 'N/A'}</b></div>
              <div>Upper: <b style={{ color: 'red' }}>{stats.upper?.toFixed(2) || 'N/A'}</b></div>
              <div>Lower: <b style={{ color: 'green' }}>{stats.lower?.toFixed(2) || 'N/A'}</b></div>
            </div>

            {/* Performance Stats */}
            <div style={{ marginBottom: '20px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', fontSize: '14px' }}>
              <div>Total Trades: <b>{stats.trades_opened || 0}</b></div>
              <div style={{ color: '#48bb78' }}>Wins: <b>{stats.wins || 0}</b></div>
              <div style={{ color: '#f56565' }}>Losses: <b>{stats.losses || 0}</b></div>
              <div>Total P&L: <b style={{ color: stats.total_pl >= 0 ? '#48bb78' : '#f56565' }}>${stats.total_pl?.toFixed(2) || '0.00'}</b></div>
            </div>

            <div style={{ display: 'flex', gap: '10px' }}>
              <button 
                onClick={() => controlBot(symbol, 'start')}
                disabled={stats.status === 'Running'}
                style={{ 
                  flex: 1, padding: '10px', cursor: 'pointer', borderRadius: '6px', border: 'none',
                  backgroundColor: stats.status === 'Running' ? '#ccc' : '#48bb78', color: 'white', fontWeight: 'bold'
                }}
              >
                Start
              </button>
              <button 
                onClick={() => controlBot(symbol, 'stop')}
                disabled={stats.status === 'Stopped'}
                style={{ 
                  flex: 1, padding: '10px', cursor: 'pointer', borderRadius: '6px', border: 'none',
                  backgroundColor: stats.status === 'Stopped' ? '#ccc' : '#f56565', color: 'white', fontWeight: 'bold'
                }}
              >
                Stop
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

export default Dashboard;
