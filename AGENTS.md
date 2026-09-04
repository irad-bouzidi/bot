# Agent Instructions

## Developer Commands
- Start DB + dashboard containers: `docker compose up -d`  (compose file is at the repo root)
- Apply the DB schema (once): `python -m backend.db.migrate`
- Check the DB: `python -m backend.db.migrate --check`
- Run Backend: `python -m backend.main`  (needs MT5 **and** Postgres)
- Run Frontend (dev server): `npm start --prefix frontend`
- Run Both (Single Terminal): `npx concurrently "python -m backend.main" "npm start --prefix frontend"`
- Tests: `python -m pytest` (passes with no MT5 and no Postgres), `npm test --prefix frontend`
- Baseline (offline, one symbol per run): `python -m backend.scripts.run_baseline --symbol XAUUSDm`
  / `--symbol BTCUSDm`. `--sl`/`--tp` default from the symbol's own config.
- Snapshot bars (MT5 host): `python -m backend.data.snapshot --symbol BTCUSDm --start 2025-09-01`
- News calendar (the only networked command): `python -m backend.live.news_feed --fetch`
  Verify it BEFORE starting a bot -- the blackout fails closed, so a dead feed shows up as
  a bot that refuses to trade. `--news-file data/news` on `run_baseline` replays with it.

## Project Architecture
- **Backend**: Python/FastAPI located in `backend/`. Manages MT5 trading bot logic.
  Not containerised — it imports `MetaTrader5`, which needs a logged-in Windows terminal.
- **Frontend**: React/TypeScript located in `frontend/`. Built and served as static
  files by a container (`frontend/Dockerfile`, no nginx), which does **not** proxy the
  API — the browser calls `127.0.0.1:8000` directly so the API can stay bound to
  loopback.
- **Database**: Postgres in a container. Holds sizing, trade history, backtest runs and
  dashboard preferences. All SQL is in `backend/db/repository.py`; the schema is
  `backend/db/schema.sql`. The research stack (`backtest/`, `strategy/`, `indicators/`,
  `data/`) must never import `backend.db`.
- **News blackout (S9)**: no entries and an immediate flatten within 30 min either side of
  a high/medium USD release. Window arithmetic in `backend/core/news.py` (stdlib only,
  shared with the backtest); the ForexFactory fetch in `backend/live/news_feed.py`, the
  only module in `backend/` allowed to import a network library. Wall-clock rule -- uses
  the host's UTC clock, never `bar_time`, which is broker-server time. Fails closed, and
  never flattens on an unknown calendar. Off with `BOT_NEWS_ENABLED=0`.
- **Symbols**: `XAUUSDm` and `BTCUSDm`, both defined in `backend/core/symbols.py`
  (`SYMBOL_CONFIG`) — the one MT5-free, database-free copy that the live loop,
  `backend.db.migrate` and the research scripts all read. Geometry is in pip
  COUNTS times a per-symbol pip size: gold 70/100 x 0.1, Bitcoin 700/1000 x 1.0.
  Adding a symbol is one entry there plus `python -m backend.db.migrate`.
- **External Dependency**: Requires MetaTrader 5 (MT5) Terminal installed and logged in.

## Setup
- Backend: `pip install -r requirements.txt`
- Frontend: `cd frontend && npm install`
- Database: `docker compose up -d db && python -m backend.db.migrate`
