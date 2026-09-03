#!/bin/sh
# Write the API base into the served page at CONTAINER START, then hand over to
# the static server.
#
# Create React App inlines every REACT_APP_* value into the bundle at build
# time, so an API base chosen at build time can only be changed by rebuilding
# the image. This writes a tiny env.js that index.html loads before the bundle,
# so the same image can point at a different host by changing one compose
# variable.
set -eu

API_BASE="${BOT_API_BASE:-http://127.0.0.1:8000}"
TARGET="/app/build/env.js"

# Trailing slash stripped here, once, because every caller in the app builds
# URLs as `${API_BASE}/stats` -- a trailing slash would produce `//stats`.
API_BASE="${API_BASE%/}"

# JSON.stringify, not a hand-rolled `sed`, because the output is a JavaScript
# string literal and that is the one function guaranteed to produce a valid one
# -- quotes, backslashes, newlines and all. The previous version escaped two
# characters with `sed 's/\\/\\\\/g; s/"/\\"/g'`, which busybox sed rejects
# outright ("bad option in substitution expression"): under nginx that error
# went to the log and was ignored, so env.js was silently never rewritten and
# BOT_API_BASE did nothing. It only looked like it worked because the built-in
# dev default happens to be the same URL.
API_BASE="$API_BASE" node -e '
  var fs = require("fs");
  fs.writeFileSync(process.argv[1],
    "// Generated at container start by frontend/docker-entrypoint.sh. Do not edit.\n" +
    "window.__BOT_CONFIG__ = { apiBase: " + JSON.stringify(process.env.API_BASE) + " };\n");
' "$TARGET"

echo "bot-api-base: serving with apiBase=${API_BASE}"

# exec, not a plain call: the server must inherit PID 1 so `docker stop` sends
# SIGTERM to something that will act on it.
exec "$@"
