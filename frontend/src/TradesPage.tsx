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

  if (error) return <p className="detail-msg err">{error}</p>;
  if (!deals) return <p className="detail-msg">Loading deals…</p>;
  if (!deals.length) return <p className="detail-msg">No deals recorded.</p>;

  return (
    // The only table in the app that keeps its own border, because it is nested
    // inside a row of another one: flush, it would read as a continuation of
    // the trade table rather than as the detail of one row of it.
    <div className="table-wrap bordered">
      <table className="data-table">
        <caption className="sr-only">
          Broker deals making up position #{positionId}
        </caption>
        <thead>
          <tr>
            <th scope="col">Deal</th>
            <th scope="col">Kind</th>
            <th scope="col">Type</th>
            <th scope="col" className="num">Volume</th>
            <th scope="col" className="num">Price</th>
            <th scope="col" className="num">Profit</th>
            <th scope="col" className="num">Costs</th>
            <th scope="col">Time</th>
            <th scope="col">Comment</th>
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
              <td className="deal-comment" title={d.comment || undefined}>
                {d.comment || '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

// `symbols` is every configured symbol; `symbol` (undefined) means all of them.
// The page used to be handed ONE symbol -- the first key of /stats -- which
// silently hid every trade on any other symbol the moment a second one existed.
const TradesPage = ({ symbols = [] }: { symbols?: string[] }) => {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState<StatusFilter>('all');
  const [symbol, setSymbol] = useState<string | undefined>(undefined);
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

  // Either filter changes what the offset means, so an offset from the previous
  // one would land on a page that no longer exists.
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
  const columns = symbols.length > 1 ? 12 : 11;

  return (
    <>
      <div className="page-head">
        <div>
          <p className="eyebrow">History</p>
          <h2 className="page-title">Trade History</h2>
          <p className="page-desc">
            One row per position, stored in Postgres. A trade that scaled out and then
            closed is <b>one</b> trade — won or lost on its net result, costs included.
            Select a row for the broker deals behind it.
          </p>
        </div>
        <button className="btn btn-outline" onClick={resync} disabled={refreshing}>
          {refreshing ? 'Re-reading…' : 'Re-read from MT5'}
        </button>
      </div>

      <section className="card">
        <div className="card-header">
          <div className="filter-bar">
            <div className="toggle-group">
              {(['all', 'open', 'closed'] as StatusFilter[]).map(s => (
                <button
                  key={s}
                  type="button"
                  className={`toggle ${status === s ? 'active' : ''}`}
                  onClick={() => setStatus(s)}
                  aria-pressed={status === s}
                >
                  {s === 'all' ? 'All' : s === 'open' ? 'Open' : 'Closed'}
                </button>
              ))}
            </div>
            {symbols.length > 1 && (
              <div className="toggle-group">
                <button
                  type="button"
                  className={`toggle ${symbol === undefined ? 'active' : ''}`}
                  onClick={() => setSymbol(undefined)}
                  aria-pressed={symbol === undefined}
                >
                  All symbols
                </button>
                {symbols.map(s => (
                  <button
                    key={s}
                    type="button"
                    className={`toggle mono ${symbol === s ? 'active' : ''}`}
                    onClick={() => setSymbol(s)}
                    aria-pressed={symbol === s}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
          <span className="badge badge-secondary">
            {total} trade{total === 1 ? '' : 's'}
          </span>
        </div>

        {error && (
          <div className="card-content">
            <p className="msg err">{error}</p>
          </div>
        )}

        {loading ? (
          <div className="sk-rows" aria-busy="true" aria-label="Loading trades">
            {[...Array(6)].map((_, i) => (
              <div key={i} className="skeleton sk-row" />
            ))}
          </div>
        ) : !trades.length ? (
          <div className="empty-state">
            <div className="empty-icon">📄</div>
            <h3>No trades recorded</h3>
            <p>
              Nothing matched this filter. If the bot has traded before, use
              “Re-read from MT5” to pull the broker’s deal history in.
            </p>
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <caption className="sr-only">
                Stored trades, one row per position. Activate a row to show the broker
                deals behind it.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Position</th>
                  {symbols.length > 1 && <th scope="col">Symbol</th>}
                  <th scope="col">Side</th>
                  <th scope="col">Status</th>
                  <th scope="col">Opened</th>
                  <th scope="col">Closed</th>
                  <th scope="col" className="num">Entry</th>
                  <th scope="col" className="num">Exit</th>
                  <th scope="col" className="num">Lots</th>
                  <th scope="col" className="num">Exits</th>
                  <th scope="col" className="num">Costs</th>
                  <th scope="col" className="num">Net</th>
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
                        className={`row-clickable ${isExpanded ? 'expanded' : ''}`}
                        // Expanding was mouse-only and announced nothing: a
                        // screen reader had no way to know the row had a
                        // detail, let alone that it was open.
                        tabIndex={0}
                        aria-expanded={isExpanded}
                        onClick={() => setExpanded(isExpanded ? null : t.position_id)}
                        onKeyDown={e => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            setExpanded(isExpanded ? null : t.position_id);
                          }
                        }}
                      >
                        <td className="mono">
                          {/* The rows were clickable with nothing on them to
                              say so. A caret is the cheapest thing that both
                              invites the click and shows the current state. */}
                          <span className="disclosure" aria-hidden="true">
                            ▶
                          </span>
                          #{t.position_id}
                        </td>
                        {symbols.length > 1 && <td className="mono">{t.symbol}</td>}
                        <td>
                          <span className={`badge badge-outline ${t.side === 'long' ? 'badge-long' : 'badge-short'}`}>
                            {t.side === 'long' ? 'Long' : 'Short'}
                          </span>
                        </td>
                        <td>
                          <span className={`badge ${open ? 'badge-secondary' : 'badge-outline'}`}>
                            {t.status}
                          </span>
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
                        <tr className="detail-row">
                          <td colSpan={columns}>
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
          <div className="card-footer pager">
            <button
              className="btn btn-outline btn-sm"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </button>
            <span className="pager-label">
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total} · page {page} of{' '}
              {pages}
            </span>
            <button
              className="btn btn-outline btn-sm"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </button>
          </div>
        )}
      </section>

      <p className="page-note">
        Open trades show no net result on purpose — a scale-out is not a close, and
        dating a trade by its partial exit would drop it into the closed-trade equity
        curve early.
      </p>
    </>
  );
};

export default TradesPage;
