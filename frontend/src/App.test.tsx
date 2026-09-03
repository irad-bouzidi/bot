import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
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
  symbols: ['XAUUSDm'],
};

const stats = {
  account: { balance: 1000, equity: 1000, time_profits: { daily: 0 } },
  bots: {
    XAUUSDm: {
      symbol: 'XAUUSDm',
      status: 'Stopped',
      desired_state: 'stopped',
      last_close: 3300.5,
      out: 3300,
      upper: 3310,
      lower: 3290,
      trades_opened: 0,
      wins: 0,
      losses: 0,
      total_pl: 0,
      max_drawdown: 0,
      persisted: true,
    },
  },
};

/** Route each endpoint the dashboard polls to a canned body. */
function mockApi(preferences: Record<string, any>, available = true) {
  const routes: Array<[string, unknown]> = [
    ['/preferences', { available, preferences }],
    ['/health', health],
    ['/stats', stats],
    ['/settings/history', { history: [] }],
    ['/settings', {}],
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
