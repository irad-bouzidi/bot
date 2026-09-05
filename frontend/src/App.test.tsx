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
  exit_at_mean: false,
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
function mockApi(
  preferences: Record<string, any>,
  available = true,
  settingsBody: Record<string, any> = settings,
  statsBody: Record<string, any> = stats,
) {
  const routes: Array<[string, unknown]> = [
    ['/preferences', { available, preferences }],
    ['/health', health],
    ['/stats', statsBody],
    ['/settings/history', { history: [] }],
    ['/settings', settingsBody],
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

test('"All assets" selects every symbol in one click', async () => {
  const fetchMock = mockApi({ theme: 'light', view: 'backtest' });
  global.fetch = fetchMock as any;

  render(<App />);

  const all = await screen.findByRole('button', { name: 'All assets' });
  // Not active while only gold is selected -- it reflects the selection rather
  // than acting as a third instrument.
  expect(all).toHaveAttribute('aria-pressed', 'false');

  fireEvent.click(all);

  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'All assets' })).toHaveAttribute(
      'aria-pressed',
      'true',
    ),
  );
  expect(screen.getByRole('button', { name: 'BTCUSDm', pressed: true })).toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Last Month' }));
  fireEvent.click(screen.getByRole('button', { name: /run combined backtest/i }));

  await waitFor(() => {
    const call = fetchMock.mock.calls.find(
      ([url, init]: any) => String(url).includes('/backtest') && init?.method === 'POST',
    );
    expect(JSON.parse((call as any)[1].body).symbols).toEqual(['XAUUSDm', 'BTCUSDm']);
  });
});

test('a failed symbol fetch says so instead of quietly showing one symbol', async () => {
  // The fallback list is one symbol. Swallowing the error renders as "this bot
  // only trades gold" -- a plausible page with no sign that anything is missing.
  const fetchMock = mockApi({ theme: 'light', view: 'backtest' });
  // mockApi's routes are chosen from the URL alone, so `init` is not forwarded --
  // passing it is a type error that `react-scripts test` (babel, no type-check)
  // does not see but `npm run build` does, which is how it reached the container.
  global.fetch = jest.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/settings') && !url.includes('/settings/history')) {
      return Promise.reject(new TypeError('Failed to fetch'));
    }
    return fetchMock(input);
  }) as any;

  render(<App />);

  expect(
    await screen.findByText(/the symbol list comes from the backend/i),
  ).toBeInTheDocument();
});

// --- the centre-line exit toggle -------------------------------------------
//
// This rule closed the scaled-out half of trades before they could reach their
// target, and until now it was hardcoded in three engines with no off-switch
// anywhere. These pin the two properties that make the switch trustworthy: it
// shows what the server actually holds, and it stays usable while a trade is
// open -- which is the only moment anybody reaches for it.

test('the centre-line exit switch shows what /settings holds, per symbol', async () => {
  global.fetch = mockApi({ theme: 'light', view: 'dashboard' }, true, {
    XAUUSDm: { ...sizing('XAUUSDm', 0.1, 70, 100), exit_at_mean: true },
    BTCUSDm: sizing('BTCUSDm', 1.0, 700, 1000),
  }) as any;

  render(<App />);

  const boxes = await screen.findAllByRole('checkbox', { name: /centre line/i });
  expect(boxes).toHaveLength(2);
  expect(boxes[0]).toBeChecked();
  expect(boxes[1]).not.toBeChecked();
});

test('toggling the exit rule posts the flag and NOT the lot size', async () => {
  // The whole reason it is a separate control: a request carrying `lot_size` is
  // refused while a position is open, so a form that always sent all three
  // fields would make the switch dead in exactly the state it matters in.
  const fetchMock = mockApi({ theme: 'light', view: 'dashboard' });
  const posted: any[] = [];
  global.fetch = jest.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/settings') && !url.includes('/settings/history') && init?.body) {
      posted.push(JSON.parse(String(init.body)));
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ ...sizing('XAUUSDm', 0.1, 70, 100), exit_at_mean: true, notes: [] }),
      } as Response);
    }
    return fetchMock(input);
  }) as any;

  render(<App />);
  const boxes = await screen.findAllByRole('checkbox', { name: /centre line/i });
  fireEvent.click(boxes[0]);

  await waitFor(() => expect(posted).toHaveLength(1));
  expect(posted[0]).toEqual({ symbol: 'XAUUSDm', exit_at_mean: true });
  expect(posted[0]).not.toHaveProperty('lot_size');
  expect(posted[0]).not.toHaveProperty('scale_out_lots');
});

test('the exit rule stays editable while the lot numbers are locked', async () => {
  const locked = {
    ...sizing('XAUUSDm', 0.1, 70, 100),
    open_positions: 1,
    locked: true,
  };
  global.fetch = mockApi({ theme: 'light', view: 'dashboard' }, true, {
    XAUUSDm: locked,
    BTCUSDm: sizing('BTCUSDm', 1.0, 700, 1000),
  }) as any;

  render(<App />);

  const lots = await screen.findAllByLabelText('Lot size');
  expect(lots[0]).toBeDisabled();
  const boxes = await screen.findAllByRole('checkbox', { name: /centre line/i });
  expect(boxes[0]).toBeEnabled();
});

test('a failed settings poll says the values on the card are stale', async () => {
  // `settings` keeps its last value on failure. A stale lot size is visible
  // against the dollar-risk figure beside it; a stale BOOLEAN is not -- a switch
  // reading "off" while the bot is running with the rule on looks exactly like
  // the truth, so the staleness has to be stated.
  const fetchMock = mockApi({ theme: 'light', view: 'dashboard' });
  global.fetch = jest.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/settings') && !url.includes('/settings/history')) {
      return Promise.reject(new TypeError('Failed to fetch'));
    }
    return fetchMock(input);
  }) as any;

  render(<App />);

  // One per bot card: the note sits on the card whose values are stale, not in a
  // single banner, because /stats already owns the "backend is down" banner.
  expect(await screen.findAllByText(/exit rules could not be refreshed/i)).toHaveLength(2);
});

// --- a frozen account reading ----------------------------------------------

test('an account snapshot MT5 stopped refreshing is labelled, not shown as live', async () => {
  // /stats serves the last stored snapshot when MT5 does not answer, which is
  // the right call -- the last known balance beats an empty panel. What is
  // pinned here is that the panel says so: the note that normally sits under
  // the grid promises a once-a-minute refresh, so an unlabelled seven-hour-old
  // reading presents a dead terminal as a live account.
  const frozen = {
    ...stats,
    account: {
      ...stats.account,
      captured_at: '2026-09-04T11:04:07+00:00',
      age_seconds: 24564,
      stale: true,
    },
  };
  const fetchMock = mockApi({ theme: 'light', view: 'dashboard' }, true, settings, frozen);
  global.fetch = fetchMock as any;

  render(<App />);

  expect(await screen.findByText(/not refreshing/i)).toBeInTheDocument();
  expect(screen.getByText(/6\.8 h old/)).toBeInTheDocument();
  expect(screen.getByText(/2026-09-04 11:04:07 UTC/)).toBeInTheDocument();
  // The reassuring version must be gone, not merely joined by the warning.
  expect(screen.queryByText(/refreshed at most once a minute/i)).not.toBeInTheDocument();
  // And the snapshot's own bookkeeping never becomes a stat card beside balance.
  expect(screen.queryByText('age seconds')).not.toBeInTheDocument();
  // Balance and equity both read 1,000, hence getAllByText: the point is that
  // the figures are still served, only no longer presented as current.
  expect(screen.getAllByText('1,000').length).toBeGreaterThan(0);
});

test('a fresh account snapshot keeps the ordinary throttle note', async () => {
  const fresh = {
    ...stats,
    account: {
      ...stats.account,
      captured_at: '2026-09-04T17:53:31+00:00',
      age_seconds: 12,
      stale: false,
    },
  };
  global.fetch = mockApi({ theme: 'light', view: 'dashboard' }, true, settings, fresh) as any;

  render(<App />);

  expect(await screen.findByText(/refreshed at most once a minute/i)).toBeInTheDocument();
  expect(screen.queryByText(/not refreshing/i)).not.toBeInTheDocument();
});

// --- actions: nothing consequential happens on one click -------------------
//
// Three of the controls on this dashboard cannot be undone by pressing the
// button again: Start places real orders, Stop can leave a live position with
// nothing managing it, and Delete drops the only record of a computed run.
// These pin that each one is a REQUEST until it is confirmed, and that the
// confirmation says which object and what it costs.

const storedRun = {
  id: 7,
  symbol: 'XAUUSDm',
  symbols: ['XAUUSDm'],
  created_at: '2026-09-01T10:20:00',
  start_date: '2026-08-01T00:00:00',
  end_date: '2026-09-01T23:59:59',
  initial_balance: 1000,
  lot_size: 0.1,
  scale_out_lots: 0.05,
  sizing: {},
  engine: 'legacy',
  status: 'ok',
  error: null,
  result: { total_pl: -12.5, win_rate: 51.2, trades_opened: 40 },
};

const storedTrade = {
  position_id: 512300,
  symbol: 'XAUUSDm',
  side: 'short',
  status: 'closed',
  opened_at: '2026-09-04T09:15:00',
  closed_at: '2026-09-04T13:20:00',
  entry_price: 4485.183,
  exit_price: 4479.196,
  volume_in: 0.1,
  volume_out: 0.05,
  exit_count: 2,
  commission: -0.21,
  swap: -0.04,
  fee: 0,
  net_profit: 12.4,
};

/** mockApi, plus canned bodies for the routes a given test needs to control. */
function mockApiWith(
  preferences: Record<string, any>,
  overrides: Array<[string, unknown]>,
  settingsBody?: Record<string, any>,
  statsBody?: Record<string, any>,
) {
  const base = mockApi(preferences, true, settingsBody, statsBody);
  const seen: Array<{ url: string; method: string; body: any }> = [];
  const fetchMock = jest.fn((input: any, init?: any) => {
    const url = String(input);
    const method = init?.method || 'GET';
    seen.push({ url, method, body: init?.body ? JSON.parse(init.body) : null });
    const hit = overrides.find(([path]) => url.includes(path));
    if (hit && method === 'GET') {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(hit[1]),
      } as Response);
    }
    if (method !== 'GET') {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ message: 'ok', deleted: 1 }),
      } as Response);
    }
    return base(input);
  });
  return { fetchMock, seen };
}

test('deleting a stored run asks first, and only then sends the DELETE', async () => {
  const { fetchMock, seen } = mockApiWith({ theme: 'light', view: 'backtest' }, [
    ['/backtests', { runs: [storedRun] }],
  ]);
  global.fetch = fetchMock as any;

  render(<App />);

  // Icon-only, so the accessible name has to identify the run -- "Delete"
  // fifteen times down a column names nothing.
  const trash = await screen.findByRole('button', {
    name: /delete the backtest run: XAUUSDm, 2026-08-01 to 2026-09-01/i,
  });
  fireEvent.click(trash);

  const dialog = await screen.findByRole('alertdialog');
  expect(dialog).toHaveTextContent(/removed for good/i);
  expect(seen.some(c => c.method === 'DELETE')).toBe(false);

  // Cancel is the safe direction, and it must really be a no-op.
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull());
  expect(seen.some(c => c.method === 'DELETE')).toBe(false);

  fireEvent.click(
    screen.getByRole('button', { name: /delete the backtest run/i }),
  );
  fireEvent.click(await screen.findByRole('button', { name: 'Delete run' }));

  await waitFor(() =>
    expect(
      seen.some(c => c.url.includes('/backtests/7') && c.method === 'DELETE'),
    ).toBe(true),
  );
});

test('Start places no order until the dialog is confirmed, and names the risk', async () => {
  const { fetchMock, seen } = mockApiWith({ theme: 'light', view: 'dashboard' }, []);
  global.fetch = fetchMock as any;

  render(<App />);

  const starts = await screen.findAllByRole('button', { name: 'Start' });
  fireEvent.click(starts[0]);

  expect(seen.some(c => c.url.includes('/control'))).toBe(false);

  const dialog = await screen.findByRole('alertdialog');
  // The dollar risk of the size it will ACTUALLY trade with, read from
  // /settings rather than assumed: 0.1 lots of gold over a 7.00 stop is ~$70.
  expect(dialog).toHaveTextContent(/~\$70/);
  expect(dialog).toHaveTextContent(/real orders/i);

  fireEvent.click(screen.getByRole('button', { name: 'Start trading' }));

  await waitFor(() =>
    expect(seen.filter(c => c.url.includes('/control')).map(c => c.body)).toEqual([
      { symbol: 'XAUUSDm', action: 'start' },
    ]),
  );
});

test('Stop is immediate with no position, and confirms when one is open', async () => {
  const running = {
    account: stats.account,
    bots: { XAUUSDm: { ...bot('XAUUSDm', 3300.5), status: 'Running' } },
  };

  // Nothing open: stopping is reversible with the button beside it, so a
  // confirmation would be a click for its own sake.
  const idle = mockApiWith(
    { theme: 'light', view: 'dashboard' },
    [],
    { XAUUSDm: sizing('XAUUSDm', 0.1, 70, 100) },
    running,
  );
  global.fetch = idle.fetchMock as any;

  const first = render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
  await waitFor(() =>
    expect(idle.seen.filter(c => c.url.includes('/control')).map(c => c.body)).toEqual([
      { symbol: 'XAUUSDm', action: 'stop' },
    ]),
  );
  expect(screen.queryByRole('alertdialog')).toBeNull();
  first.unmount();

  // A position open: stopping strands it -- the broker-side SL/TP stay, but
  // nothing will fire the scale-out or pull the stop to break-even.
  const held = mockApiWith(
    { theme: 'light', view: 'dashboard' },
    [],
    { XAUUSDm: { ...sizing('XAUUSDm', 0.1, 70, 100), open_positions: 1, locked: true } },
    running,
  );
  global.fetch = held.fetchMock as any;

  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));

  const dialog = await screen.findByRole('alertdialog');
  expect(dialog).toHaveTextContent(/does not close the 1 open position/i);
  expect(held.seen.some(c => c.url.includes('/control'))).toBe(false);
});

test('a trade row expands from the keyboard and reports whether it is open', async () => {
  // It was a <tr onClick> with no tabIndex and no aria-expanded, so the deals
  // behind every trade were mouse-only and a screen reader had no way to know
  // the row had a detail at all.
  const { fetchMock } = mockApiWith({ theme: 'light', view: 'trades' }, [
    ['/trades/512300', { position_id: 512300, deals: [] }],
    ['/trades', { trades: [storedTrade], total: 1, limit: 50, offset: 0 }],
  ]);
  global.fetch = fetchMock as any;

  render(<App />);

  await screen.findByText('Trade History');
  const row = (await waitFor(() => {
    const found = screen
      .getAllByRole('row')
      .find(r => r.getAttribute('aria-expanded') !== null);
    if (!found) throw new Error('no expandable row');
    return found;
  })) as HTMLElement;

  expect(row).toHaveAttribute('aria-expanded', 'false');
  expect(row).toHaveAttribute('tabindex', '0');

  fireEvent.keyDown(row, { key: 'Enter' });
  await waitFor(() => expect(row).toHaveAttribute('aria-expanded', 'true'));

  fireEvent.keyDown(row, { key: ' ' });
  await waitFor(() => expect(row).toHaveAttribute('aria-expanded', 'false'));
});
