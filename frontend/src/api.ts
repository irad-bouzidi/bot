// The single place the frontend knows a URL, and the single place it knows how
// this backend reports failure.
//
// Every endpoint here answers 200 with an `{ error }` body rather than an HTTP
// status -- that is the existing convention across /settings, /backtest and the
// rest, and callers that only checked `res.ok` were silently treating a refusal
// as a success. `request()` folds both shapes into one thrown Error.

declare global {
  interface Window {
    __BOT_CONFIG__?: { apiBase?: string };
  }
}

// Runtime, not build time: public/env.js is regenerated on every container
// start from BOT_API_BASE, so one image can point at a different host. A CRA
// REACT_APP_* value would be inlined into the bundle and need a rebuild.
export const API_BASE = (
  window.__BOT_CONFIG__?.apiBase || 'http://127.0.0.1:8000'
).replace(/\/$/, '');

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, init);
  } catch (e) {
    throw new ApiError(`Could not reach the backend at ${API_BASE}.`);
  }
  if (!res.ok) {
    throw new ApiError(`${init?.method || 'GET'} ${path} failed (${res.status}).`);
  }
  const body = await res.json();
  if (body && typeof body === 'object' && 'error' in body && body.error) {
    throw new ApiError(String(body.error));
  }
  return body as T;
}

const postJson = <T,>(path: string, payload: unknown) =>
  request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

// ---------------------------------------------------------------------------
// types
// ---------------------------------------------------------------------------

export interface Health {
  database: {
    url: string;
    reachable: boolean;
    schema_version: number;
    tables_present: boolean;
    migrate_command: string;
  };
  auto_resume: boolean;
  symbols: string[];
}

export interface Trade {
  position_id: number;
  symbol: string;
  side: 'long' | 'short';
  status: 'open' | 'closed';
  opened_at: string;
  closed_at: string | null;
  entry_price: number | null;
  exit_price: number | null;
  volume_in: number;
  volume_out: number;
  exit_count: number;
  gross_profit: number;
  commission: number;
  swap: number;
  fee: number;
  net_profit: number;
  comment: string | null;
}

export interface BacktestRun {
  id: number;
  engine: string;
  symbol: string;
  start_date: string;
  end_date: string;
  initial_balance: number;
  lot_size: number | null;
  scale_out_lots: number | null;
  partial_fraction: number | null;
  status: 'ok' | 'error';
  error: string | null;
  result: any | null;
  duration_ms: number | null;
  created_at: string;
}

export interface SettingsChange {
  id: number;
  symbol: string;
  lot_size: number;
  partial_fraction: number;
  prev_lot_size: number | null;
  prev_partial_fraction: number | null;
  source: string;
  notes: string | null;
  created_at: string;
}

// `available` distinguishes "nothing saved yet" from "the store is down". The
// difference matters: on a failed read the app must NOT write its defaults
// back, or one bad poll would overwrite a theme that is stored fine.
export interface PreferencesResponse {
  available: boolean;
  preferences: Record<string, any>;
  error?: string;
}

// ---------------------------------------------------------------------------
// endpoints
// ---------------------------------------------------------------------------

export const getHealth = () => request<Health>('/health');

export const getStats = () => request<any>('/stats');

export const getSettings = () => request<Record<string, any>>('/settings');

export const saveSizing = (symbol: string, lotSize: number, scaleOutLots: number) =>
  postJson<any>('/settings', {
    symbol,
    lot_size: lotSize,
    scale_out_lots: scaleOutLots,
  });

export const getSettingsHistory = (symbol?: string, limit = 20) =>
  request<{ history: SettingsChange[] }>(
    `/settings/history?limit=${limit}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ''}`,
  );

export const controlBot = (symbol: string, action: 'start' | 'stop') =>
  postJson<{ message: string }>('/control', { symbol, action });

export const getTrades = (params: {
  symbol?: string;
  status?: 'open' | 'closed';
  limit?: number;
  offset?: number;
} = {}) => {
  const q = new URLSearchParams();
  if (params.symbol) q.set('symbol', params.symbol);
  if (params.status) q.set('status', params.status);
  q.set('limit', String(params.limit ?? 100));
  q.set('offset', String(params.offset ?? 0));
  return request<{ trades: Trade[]; total: number; limit: number; offset: number }>(
    `/trades?${q.toString()}`,
  );
};

export const getTradeDeals = (positionId: number) =>
  request<{ position_id: number; deals: any[] }>(`/trades/${positionId}`);

export const refreshTrades = (symbol?: string, full = false) =>
  postJson<{ refreshed: Record<string, boolean> }>(
    `/trades/refresh?full=${full}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ''}`,
    {},
  );

export const runBacktest = (payload: Record<string, unknown>) =>
  postJson<any>('/backtest', payload);

export const getBacktests = (symbol?: string, limit = 25) =>
  request<{ runs: BacktestRun[] }>(
    `/backtests?limit=${limit}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ''}`,
  );

export const deleteBacktest = (id: number) =>
  request<{ deleted: number }>(`/backtests/${id}`, { method: 'DELETE' });

export const getPreferences = () => request<PreferencesResponse>('/preferences');

// A PATCH in behaviour: the server merges. The theme switch and the backtest
// form both write here, and a replace would let whichever fired last erase the
// other's fields.
export const savePreferences = (data: Record<string, any>) =>
  postJson<PreferencesResponse>('/preferences', { data });
