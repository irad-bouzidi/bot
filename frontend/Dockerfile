# Build the CRA bundle, then serve it as static files. No nginx: the container
# does one job -- hand over the page -- and `serve` does that in the runtime the
# build stage already needs, with the SPA fallback, cache rules and security
# headers declared in serve.json.
#
# Build context is frontend/ (see docker-compose.yml), so every COPY below is
# relative to this directory.

# ---- build ------------------------------------------------------------------
# node:24 to match the npm major that produced package-lock.json
# (lockfileVersion 3, npm 11). node:20 ships npm 10, which reads the same lock
# file and rejects it -- "Missing: yaml@2.9.0 from lock file" -- because the two
# majors resolve that transitive dependency differently. `npm ci` is worth
# keeping over `npm install` for a container build, so the image moves rather
# than the guarantee.
FROM node:24-alpine AS build

WORKDIR /app

# Copied before the sources so that a change to a component does not invalidate
# the dependency layer -- `npm ci` is the slow step by an order of magnitude.
COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json ./
COPY public ./public
COPY src ./src

# CI=true makes CRA treat warnings as errors, which is wrong for a build whose
# job is to ship what the repo currently says. Docker sets CI=true itself in
# some builders, so it is unset explicitly rather than left to chance.
ENV CI=false
# Sourcemaps roughly double the image's static payload and are of no use in a
# container nobody debugs into.
ENV GENERATE_SOURCEMAP=false
RUN npm run build


# ---- serve ------------------------------------------------------------------
FROM node:24-alpine

# Pinned exactly. An unpinned static server is still a static server that
# answers on the page holding this account's start/stop controls.
RUN npm install -g serve@14.2.6

WORKDIR /app

COPY --from=build /app/build ./build
# Kept OUTSIDE ./build on purpose. serve's default config location is
# <public>/serve.json, which would also make the file fetchable at /serve.json;
# --config points at it here instead so the cache and header rules are not
# themselves served.
COPY serve.json ./serve.json
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# ./build must be writable by the runtime user: the entrypoint rewrites env.js
# there on every start. Everything else stays root-owned and read-only to the
# process serving the internet-facing socket.
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && chown -R node:node ./build

# nginx needed root to bind :80. A static server on :3000 does not, so it does
# not get it -- the port is unprivileged and mapped 1:1 by compose.
USER node

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD wget -qO- http://127.0.0.1:3000/ >/dev/null 2>&1 || exit 1

# ENTRYPOINT writes env.js and then `exec`s this CMD, so `serve` is PID 1 and
# `docker stop` reaches it rather than timing out into SIGKILL.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["serve", "--config", "/app/serve.json", "--listen", "tcp://0.0.0.0:3000", \
     "--no-clipboard", "--no-port-switching", "/app/build"]
