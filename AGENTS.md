# Agent Instructions

## Developer Commands
- Start DB + dashboard containers: `docker compose -f docker/docker-compose.yml up -d`
- Apply the DB schema (once): `python -m backend.db.migrate`
- Check the DB: `python -m backend.db.migrate --check`
- Run Backend: `python -m backend.main`  (needs MT5 **and** Postgres)
- Run Frontend (dev server): `npm start --prefix frontend`
- Run Both (Single Terminal): `npx concurrently "python -m backend.main" "npm start --prefix frontend"`
- Tests: `python -m pytest` (passes with no MT5 and no Postgres), `npm test --prefix frontend`

## Project Architecture
- **Backend**: Python/FastAPI located in `backend/`. Manages MT5 trading bot logic.
  Not containerised — it imports `MetaTrader5`, which needs a logged-in Windows terminal.
- **Frontend**: React/TypeScript located in `frontend/`. Served by nginx in a container,
  which does **not** proxy the API — the browser calls `127.0.0.1:8000` directly so the
  API can stay bound to loopback.
- **Database**: Postgres in a container. Holds sizing, trade history, backtest runs and
  dashboard preferences. All SQL is in `backend/db/repository.py`; the schema is
  `backend/db/schema.sql`. The research stack (`backtest/`, `strategy/`, `indicators/`,
  `data/`) must never import `backend.db`.
- **External Dependency**: Requires MetaTrader 5 (MT5) Terminal installed and logged in.

## Setup
- Backend: `pip install -r requirements.txt`
- Frontend: `cd frontend && npm install`
- Database: `docker compose -f docker/docker-compose.yml up -d db && python -m backend.db.migrate`
