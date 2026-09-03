# Build the CRA bundle, then serve it from nginx.
#
# Build context is the REPO ROOT (see docker-compose.yml), so that this file can
# be read from docker/ while the frontend it builds lives in frontend/.

# ---- build ------------------------------------------------------------------
# node:24 to match the npm major that produced frontend/package-lock.json
# (lockfileVersion 3, npm 11). node:20 ships npm 10, which reads the same lock
# file and rejects it -- "Missing: yaml@2.9.0 from lock file" -- because the two
# majors resolve that transitive dependency differently. `npm ci` is worth
# keeping over `npm install` for a container build, so the image moves rather
# than the guarantee.
FROM node:24-alpine AS build

WORKDIR /app

# Copied before the sources so that a change to a component does not invalidate
# the dependency layer -- `npm ci` is the slow step by an order of magnitude.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/tsconfig.json ./
COPY frontend/public ./public
COPY frontend/src ./src

# CI=true makes CRA treat warnings as errors, which is wrong for a build whose
# job is to ship what the repo currently says. Docker sets CI=true itself in
# some builders, so it is unset explicitly rather than left to chance.
ENV CI=false
# Sourcemaps roughly double the image's static payload and are of no use in a
# container nobody debugs into.
ENV GENERATE_SOURCEMAP=false
RUN npm run build


# ---- serve ------------------------------------------------------------------
FROM nginx:1.27-alpine

COPY --from=build /app/build /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
# Included by every location in nginx.conf -- see that file for why the security
# headers cannot just live in the server block.
COPY docker/nginx-security-headers.conf /etc/nginx/security-headers.conf
COPY docker/frontend-entrypoint.sh /docker-entrypoint.d/40-bot-api-base.sh

# nginx:alpine runs every executable script in /docker-entrypoint.d before
# starting, which is how BOT_API_BASE reaches the page without a rebuild.
RUN chmod +x /docker-entrypoint.d/40-bot-api-base.sh

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
