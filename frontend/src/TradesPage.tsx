import React, { useCallback, useEffect, useState } from 'react';
import { Trade, getTradeDeals, getTrades, refreshTrades } from './api';

// The persisted trade history, one row per POSITION.
//
// Positions and not closing deals, which is the whole reason this page can be
// trusted: the old stats counted every closing deal as its own win or loss, so a
// trade that banked a scale-out and then stopped at break-even showed up as a
// win plus a flat -- two outcomes for one trade, with the win rate lifted by
// exactly the rule the cached gold data measures as NEGATIVE for expectancy.
// Here it is one row with `exit_count` 2, won or lost on its NET result.

const PAGE_SIZE = 50;

type StatusFilter = 'all' | 'open' | 'closed';

const money = (n: number | null | undefined) =>
  n === null || n === undefined ? '—' : `${n < 0 ? '-' : ''}$${Math.abs(n).toFixed(2)}`;

const price = (n: number | null | undefined, digits = 2) =>
  n === null || n === undefined ? '—' : n.toFixed(digits);

const when = (iso: string | null) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 16).replace('T', ' ');
};

const DealRows = ({ positionId }: { positionId: number }) => {
  const [deals, setDeals] = useState<any[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getTradeDeals(positionId);
        if (!cancelled) setDeals(res.deals);
      } catch (e: any) {
        if (!cancelled) setError(e?.message || 'Could not load the deals.');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [positionId]);

  if (error) return <p className="trade-detail-msg err">{error}</p>;
  if (!deals) return <p className="trade-detail-msg">Loading deals…</p>;
  if (!deals.length) return <p className="trade-detail-msg">No deals recorded.</p>;

  return (
    <table className="deal-table">
      <thead>
        <tr>
          <th>Deal</th>
          <th>Kind</th>
          <th>Type</th>
          <th className="num">Volume</th>
          <th className="num">Price</th>
          <th className="num">Profit</th>
          <th className="num">Costs</th>
          <th>Time</th>
          <th>Comment</th>
        </tr>
      </thead>
      <tbody>
        {deals.map(d => (
          <tr key={d.ticket}>
            <td className="mono">#{d.ticket}</td>
            <td>{d.entry_kind}</td>
            <td>{d.deal_type}</td>
            <td className="num">{d.volume.toFixed(2)}</td>
            <td className="num mono">{price(d.price)}</td>
            <td className={`num ${d.profit >= 0 ? 'positive' : 'negative'}`}>{money(d.profit)}</td>
            <td className="num">{money(d.commission + d.swap + d.fee)}</td>
            <td>{when(d.dealt_at)}</td>
            <td className="deal-comment">{d.comment || '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
};

const TradesPage = ({ symbol }: { symbol?: string }) => {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState<StatusFilter>('all');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await getTrades({
        symbol,
        status: status === 'all' ? undefined : status,
        limit: PAGE_SIZE,
        offset,
      });
      setTrades(res.trades);
      setTotal(res.total);
    } catch (e: any) {
      setError(e?.message || 'Could not load the trade history.');
    } finally {
      setLoading(false);
    }
  }, [symbol, status, offset]);

  useEffect(() => {
    load();
  }, [load]);

  // The filter changes what the offset means, so an offset from the previous
  // filter would land on a page that no longer exists.
  useEffect(() => {
    setOffset(0);
  }, [status, symbol]);

  const resync = async () => {
    setRefreshing(true);
    setError(null);
    try {
      await refreshTrades(symbol, false);
      await load();
    } catch (e: any) {
      setError(e?.message || 'Could not re-read the deal history.');
    } finally {
      setRefreshing(false);
    }
  };

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="trades-container">
      <div className="trades-card">
        <p className="eyebrow">History</p>
        <div className="trades-head">
          <div>
            <h2>Trade History</h2>
            <p className="trades-sub">
              One row per position, stored in Postgres. A trade that scaled out and
              then closed is <b>one</b> trade — won or lost on its net result, costs
              included.
            </p>
          </div>
          <button className="btn btn-reset" onClick={resync} disabled={refreshing}>
            {refreshing ? 'Re-reading…' : 'Re-read from MT5'}
          </button>
        </div>

        <div className="trades-filters">
          {(['all', 'open', 'closed'] as StatusFilter[]).map(s => (
            <button
              key={s}
              className={`preset-btn ${status === s ? 'active' : ''}`}
              onClick={() => setStatus(s)}
            >
              {s === 'all' ? 'All' : s === 'open' ? 'Open' : 'Closed'}
            </button>
          ))}
          <span className="trades-count">
            {total} trade{total === 1 ? '' : 's'}
          </span>
        </div>

        {error && <div className="error-msg">{error}</div>}

        {loading ? (
          <p className="trade-detail-msg">Loading…</p>
        ) : !trades.length ? (
          <div className="empty-state-card">
            <div className="empty-icon">📄</div>
            <h3>No trades recorded</h3>
            <p>
              Nothing matched this filter. If the bot has traded before, use
              “Re-read from MT5” to pull the broker’s deal history in.
            </p>
          </div>
        ) : (
          <div className="table-scroll">
            <table className="trade-table">
              <thead>
                <tr>
                  <th>Position</th>
                  <th>Side</th>
                  <th>Status</th>
                  <th>Opened</th>
                  <th>Closed</th>
                  <th className="num">Entry</th>
                  <th className="num">Exit</th>
                  <th className="num">Lots</th>
                  <th className="num">Exits</th>
                  <th className="num">Costs</th>
                  <th className="num">Net</th>
                </tr>
              </thead>
              <tbody>
                {trades.map(t => {
                  const costs = t.commission + t.swap + t.fee;
                  const open = t.status === 'open';
                  const isExpanded = expanded === t.position_id;
                  return (
                    <React.Fragment key={t.position_id}>
                      <tr
                        className={`trade-row ${isExpanded ? 'expanded' : ''}`}
                        onClick={() => setExpanded(isExpanded ? null : t.position_id)}
                      >
                        <td className="mono">#{t.position_id}</td>
                        <td>
                          <span className={`side-badge ${t.side}`}>
                            {t.side === 'long' ? 'Long' : 'Short'}
                          </span>
                        </td>
                        <td>
                          <span className={`trade-status ${t.status}`}>{t.status}</span>
                        </td>
                        <td>{when(t.opened_at)}</td>
                        <td>{when(t.closed_at)}</td>
                        <td className="num mono">{price(t.entry_price)}</td>
                        <td className="num mono">{price(t.exit_price)}</td>
                        <td className="num">
                          {t.volume_in.toFixed(2)}
                          {t.volume_out > 0 && t.volume_out < t.volume_in
                            ? ` (−${t.volume_out.toFixed(2)})`
                            : ''}
                        </td>
                        <td className="num">
                          {t.exit_count}
                          {/* exit_count > 1 IS the scale-out having fired. Marked,
                              because the rule is measured negative for expectancy
                              and its footprint should be visible per trade. */}
                          {t.exit_count > 1 ? <span className="scaled-flag" title="Scaled out">◆</span> : null}
                        </td>
                        <td className="num">{money(costs)}</td>
                        <td className={`num strong ${open ? '' : t.net_profit >= 0 ? 'positive' : 'negative'}`}>
                          {open ? '—' : money(t.net_profit)}
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr className="trade-detail-row">
                          <td colSpan={11}>
                            <DealRows positionId={t.position_id} />
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {pages > 1 && (
          <div className="pager">
            <button
              className="preset-btn"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </button>
            <span className="pager-label">
              Page {page} of {pages}
            </span>
            <button
              className="preset-btn"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </button>
          </div>
        )}

        <p className="result-note">
          Open trades show no net result on purpose — a scale-out is not a close, and
          dating a trade by its partial exit would drop it into the closed-trade
          equity curve early. Click a row for the underlying deals.
        </p>
      </div>
    </div>
  );
};

export default TradesPage;
