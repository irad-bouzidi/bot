# Agent Instructions

## Developer Commands
- Run Backend: `python -m backend.main`
- Run Frontend: `npm start --prefix frontend`
- Run Both (Single Terminal): `npx concurrently "python -m backend.main" "npm start --prefix frontend"`

## Project Architecture
- **Backend**: Python/FastAPI located in `backend/`. Manages MT5 trading bot logic.
- **Frontend**: React/TypeScript located in `frontend/`.
- **External Dependency**: Requires MetaTrader 5 (MT5) Terminal installed and logged in.

## Setup
- Backend: `pip install -r requirements.txt`
- Frontend: `cd frontend && npm install`
