import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from './App';

// Replaces the stock CRA "renders learn react link" test, which had never
// passed against this dashboard -- nothing here has ever said "learn react".
//
// What is pinned instead is the one thing that is easy to get wrong now that
// the theme and the active view come from Postgres rather than localStorage:
// the load is asynchronous, so for a moment the app holds its own defaults. If
// it painted them, a user with a stored dark theme would see a flash of light
// and, worse, a write in that window would overwrite the stored value with the
// default. So the app must not render its shell until the first /preferences
// read has returned, and must then apply what came back.

const health = {
  database: {
    url: 'postgresql://bot:***@127.0.0.1:5432/tradingbot',
    reachable: true,
    schema_version: 1,
    tables_present: true,
    migrate_command: 'python -m backend.db.migrate',
  },
  auto_resume: false,
  symbols: ['XAUUSDm', 'BTCUSDm'],
};

/** One symbol's /settings row, in the lots the dashboard edits. */
const sizing = (symbol: string, pip: number, slPips: number, tpPips: number) => ({
  symbol,
  lot_size: 0.1,
  partial_fraction: 0.5,
  scale_out_lots: 0.05,
  runner_lots: 0.05,
  splittable: true,
  be_trigger_pips: tpPips / 2,
  sl_pips: slPips,
  tp_pips: tpPips,
  pip,
  risk_per_lot: slPips * pip * (symbol === 'XAUUSDm' ? 100 : 1),
  volume_min: 0.01,
  volume_max: 200,
  volume_step: 0.01,
  broker_limits: true,
  open_positions: 0,
  locked: false,
});

const settings = {
  XAUUSDm: sizing('XAUUSDm', 0.1, 70, 100),
  BTCUSDm: sizing('BTCUSDm', 1.0, 700, 1000),
};

/** One bot's /stats card. */
const bot = (symbol: string, close: number) => ({
  symbol,
  status: 'Stopped',
  desired_state: 'stopped',
  last_close: close,
  out: close,
  upper: close * 1.01,
  lower: close * 0.99,
  trades_opened: 0,
  wins: 0,
  losses: 0,
  total_pl: 0,
  max_drawdown: 0,
  persisted: true,
});

const stats = {
  account: { balance: 1000, equity: 1000, time_profits: { daily: 0 } },
  bots: {
    XAUUSDm: bot('XAUUSDm', 3300.5),
    BTCUSDm: bot('BTCUSDm', 81000),
  },
};

/** Route each endpoint the dashboard polls to a canned body. */
function mockApi(preferences: Record<string, any>, available = true) {
  const routes: Array<[string, unknown]> = [
    ['/preferences', { available, preferences }],
    ['/health', health],
    ['/stats', stats],
    ['/settings/history', { history: [] }],
    ['/settings', settings],
    ['/trades', { trades: [], total: 0, limit: 100, offset: 0 }],
    ['/backtests', { runs: [] }],
  ];
  return jest.fn((input: RequestInfo | URL) => {
    const url = String(input);
    // Longest match first: '/settings/history' has to win over '/settings'.
    const hit = routes.find(([path]) => url.includes(path));
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(hit ? hit[1] : {}),
    } as Response);
  });
}

beforeEach(() => {
  document.documentElement.removeAttribute('data-theme');
});

afterEach(() => {
  jest.restoreAllMocks();
});

test('applies the theme stored in Postgres, not the default', async () => {
  global.fetch = mockApi({ theme: 'dark', view: 'dashboard' }) as any;

  render(<App />);

  // The default is light. If the shell rendered before /preferences returned,
  // this would be 'light' first and only later flip.
  await waitFor(() =>
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark'),
  );
  expect(document.documentElement.getAttribute('data-theme')).not.toBe('light');
});

test('opens on the view stored in Postgres', async () => {
  global.fetch = mockApi({ theme: 'light', view: 'trades' }) as any;

  render(<App />);

  // The Trades page is only mounted when the stored view selects it, so its
  // heading appearing is the assertion.
  expect(await screen.findByText('Trade History')).toBeInTheDocument();
});

test('says so when preferences are not being persisted', async () => {
  global.fetch = mockApi({}, false) as any;

  render(<App />);

  expect(
    await screen.findByText(/preferences are not being saved/i),
  ).toBeInTheDocument();
});

test('names the migrate command when the schema is missing', async () => {
  const fetchMock = mockApi({ theme: 'light', view: 'dashboard' });
  global.fetch = jest.fn((input: RequestInfo | URL) => {
    if (String(input).includes('/health')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            ...health,
            database: { ...health.database, tables_present: false, schema_version: 0 },
          }),
      } as Response);
    }
    return fetchMock(input);
  }) as any;

  render(<App />);

  // The point of the banner: an operator should not have to go and look up how
  // to fix it.
  expect(
    await screen.findByText('python -m backend.db.migrate'),
  ).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Two configured symbols
// ---------------------------------------------------------------------------
//
// Everything below is about the failure mode a second symbol introduces: the
// dashboard was written when XAUUSDm was the only one, so several places quietly
// took "the first symbol" to mean "the symbol". Those places do not throw when
// they are wrong -- they just show one instrument's numbers and omit the other's.

test('every configured symbol gets its own bot card', async () => {
  global.fetch = mockApi({ theme: 'light', view: 'dashboard' }) as any;

  render(<App />);

  expect(await screen.findByRole('heading', { name: 'XAUUSDm' })).toBeInTheDocument();
  expect(await screen.findByRole('heading', { name: 'BTCUSDm' })).toBeInTheDocument();
});

test('the risk shown per card comes from that symbol\'s own stop', async () => {
  // 0.1 lots is ~$70 on both, but by different arithmetic: gold is 7.00 of price
  // over 100 oz/lot, Bitcoin 700.00 over 1 BTC/lot. A card that reused the other
  // symbol's pip would be out by a factor of a hundred and still render.
  global.fetch = mockApi({ theme: 'light', view: 'dashboard' }) as any;

  render(<App />);

  const risks = await screen.findAllByText('~$70 at risk / trade');
  expect(risks).toHaveLength(2);
});

test('the trades page offers a per-symbol filter once there are two', async () => {
  // It used to be handed symbols[0] and filtered every request by it, so a
  // second symbol's trades were unreachable -- and the page gave no sign of it.
  global.fetch = mockApi({ theme: 'light', view: 'trades' }) as any;

  render(<App />);

  expect(await screen.findByText('Trade History')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'All symbols' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'BTCUSDm' })).toBeInTheDocument();
});

test('the backtest page runs both symbols on one account', async () => {
  const fetchMock = mockApi({ theme: 'light', view: 'backtest' });
  global.fetch = fetchMock as any;

  render(<App />);

  // Both symbols are offered, and gold is selected by default.
  const gold = await screen.findByRole('button', { name: 'XAUUSDm', pressed: true });
  const btc = screen.getByRole('button', { name: 'BTCUSDm', pressed: false });
  expect(gold).toBeInTheDocument();

  fireEvent.click(btc);

  // The button says what it is about to do -- a combined run is not the same
  // question as two runs, so the label is not incidental.
  const run = await screen.findByRole('button', { name: /run combined backtest/i });

  // A sizing row per symbol: lots are not comparable across symbols, so one
  // shared pair would be a different bet on each.
  await waitFor(() =>
    expect(screen.getAllByText('Lot size')).toHaveLength(2),
  );

  // Set the window the way a user does. The form refuses to run without one.
  fireEvent.click(screen.getByRole('button', { name: 'Last Month' }));
  fireEvent.click(run);

  await waitFor(() => {
    const call = fetchMock.mock.calls.find(
      ([url, init]: any) => String(url).includes('/backtest') && init?.method === 'POST',
    );
    expect(call).toBeTruthy();
    const body = JSON.parse((call as any)[1].body);
    expect(body.symbols).toEqual(['XAUUSDm', 'BTCUSDm']);
    // Per symbol, in lots. The backend converts the scale-out against THAT
    // symbol's lot size; sending one flat pair would resolve it against the
    // wrong one.
    expect(body.sizing).toEqual([
      { symbol: 'XAUUSDm', lot_size: 0.1, scale_out_lots: 0.05 },
      { symbol: 'BTCUSDm', lot_size: 0.1, scale_out_lots: 0.05 },
    ]);
  });
});

test('the last selected symbol cannot be unselected', async () => {
  // A run with no symbol is not a shorter run, it is no run; the backend refuses
  // it, so the form must not be able to reach that state.
  global.fetch = mockApi({ theme: 'light', view: 'backtest' }) as any;

  render(<App />);

  const gold = await screen.findByRole('button', { name: 'XAUUSDm', pressed: true });
  fireEvent.click(gold);

  expect(screen.getByRole('button', { name: 'XAUUSDm', pressed: true })).toBeInTheDocument();
});
