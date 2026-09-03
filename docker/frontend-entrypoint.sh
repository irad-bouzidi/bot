#!/bin/sh
# Write the API base into the served page at CONTAINER START.
#
# Create React App inlines every REACT_APP_* value into the bundle at build
# time, so an API base chosen at build time can only be changed by rebuilding
# the image. This writes a tiny env.js that index.html loads before the bundle,
# so the same image can point at a different host by changing one compose
# variable.
#
# Runs from /docker-entrypoint.d, which nginx:alpine executes before starting.
set -eu

API_BASE="${BOT_API_BASE:-http://127.0.0.1:8000}"
TARGET="/usr/share/nginx/html/env.js"

# Trailing slash stripped here, once, because every caller in the app builds
# URLs as `${API_BASE}/stats` -- a trailing slash would produce `//stats`.
API_BASE="${API_BASE%/}"

# JSON-escape the two characters that could break out of the string literal.
ESCAPED=$(printf '%s' "$API_BASE" | sed 's/\\/\\\\/g; s/"/\\"/g')

cat > "$TARGET" <<EOF
// Generated at container start by docker/frontend-entrypoint.sh. Do not edit.
window.__BOT_CONFIG__ = { apiBase: "${ESCAPED}" };
EOF

echo "bot-api-base: serving with apiBase=${API_BASE}"
