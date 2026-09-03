# Containers: frontend + Postgres

The backend is **not** here and cannot be. `backend/bot_manager.py` imports
`MetaTrader5`, which is Windows-only and needs a logged-in terminal on the same
machine; containerising it would mean shipping a Windows image with a GUI
trading terminal inside. So the split is the one the architecture already had —
MT5 and the API on the trading host, everything that does not need a terminal in
a container.

## Start

```powershell
cp docker/.env.example docker/.env          # optional; the defaults work
docker compose -f docker/docker-compose.yml up -d
python -m backend.db.migrate                # from the repo root, once
python -m backend.main                      # on the MT5 host, as before
```

Then open <http://localhost:3000>.

| | Runs where | Published on |
|---|---|---|
| `db` (postgres:16-alpine) | container | `127.0.0.1:5432` |
| `frontend` (nginx) | container | `127.0.0.1:3000` |
| API (`python -m backend.main`) | **host** — needs MT5 | `127.0.0.1:8000` |

## Why nginx does not proxy the API

The browser loads the page from nginx on `:3000` and calls the API **directly**
on `127.0.0.1:8000`. nginx serves static files and nothing else.

A `/api` proxy would be the conventional layout, and it is the wrong one here.
For nginx to reach the API, the API would have to be reachable from the Docker
bridge network — i.e. bound off `127.0.0.1` — and `POST /control` starts live
trading with real money with no authentication at all, as does `POST /settings`
for the size of the orders it sends. The loopback bind is the only thing
protecting either. Adding the container's published origin to the backend's CORS
list costs nothing by comparison, and `BOT_ALLOWED_ORIGINS` already defaults to
including `:3000`.

The cost is that the dashboard only works from a browser on the MT5 host — which
is the constraint the loopback bind already imposed.

Both published ports are bound to `127.0.0.1` for the same reason. `- "5432:5432"`
would bind `0.0.0.0` and put the database holding `lot_size` on every interface
behind a password that defaults to `bot`.

## Why the API base is injected at runtime

Create React App inlines every `REACT_APP_*` value into the bundle at build
time, so an API URL chosen at build time can only be changed by rebuilding the
image. Instead `docker/frontend-entrypoint.sh` writes
`/usr/share/nginx/html/env.js` from `BOT_API_BASE` on every container start, and
`index.html` loads it before the bundle. `frontend/src/api.ts` reads
`window.__BOT_CONFIG__.apiBase`. `frontend/public/env.js` is the dev default for
`npm start`.

`env.js` and `index.html` are served with `no-store`: a cached `env.js` would
pin the dashboard to a previous `BOT_API_BASE` and it would silently poll the
wrong host.

## The schema

`docker-compose.yml` mounts `../backend/db/schema.sql` into
`/docker-entrypoint-initdb.d`, so a **fresh** database gets the schema on first
boot. It is mounted rather than copied, because a copy under `docker/` would
drift from the real file the first time a column changed.

Postgres ignores that directory once the data volume exists, so
`python -m backend.db.migrate` is the path that works every time. It is
idempotent — every statement in `schema.sql` is `IF NOT EXISTS` — so running
both is harmless. `migrate` also:

- imports `data/settings.json` into `symbol_settings` **once**, so a lot size
  someone lowered on purpose is not lost in the move to Postgres. A re-run
  leaves the database value alone rather than restoring the file's;
- seeds a row for any configured symbol that has none, so `/settings` never has
  to answer "no row" — the fallback for which would be the 0.1 code default.

`python -m backend.db.migrate --check` reports connectivity and schema version
and changes nothing.

## Node version

The build stage is `node:24-alpine`, matching the npm major that produced
`frontend/package-lock.json` (lockfileVersion 3, npm 11). `node:20` ships npm 10,
which reads the same lock file and rejects it — *"Missing: yaml@2.9.0 from lock
file"* — because the two majors resolve that transitive dependency differently.
`npm ci` is worth keeping over `npm install` for a container build, so the image
moved rather than the guarantee.

## Security headers

Every `location` in `nginx.conf` does `include /etc/nginx/security-headers.conf`
rather than inheriting the headers from the `server` block. This is not
redundancy: nginx's `add_header` **replaces** the inherited set rather than
merging with it, and every location here sets its own `Cache-Control` — so
server-level `X-Frame-Options` reached none of them. Verified with
`curl -D -` on `/`, `/env.js`, `/index.html`, `/static/…` and an SPA route.

## Data

Postgres data lives in the named volume `docker_db-data`. It survives
`docker compose down`. `docker compose down -v` **deletes it**, including the
trade history and the stored lot size.

```powershell
# back it up first
docker exec nw-bot-db pg_dump -U bot tradingbot > backup.sql
```
