#!/bin/sh
# Starts EternalChat's web page in the container (Dockerfile). A first boot
# leaves subscription settings on the volume, Codex by default; the page's
# Settings changes them, and the change is kept there too. With an
# ANTHROPIC_API_KEY secret it starts on the API instead.
set -eu
mkdir -p "$ETERNALCHAT_HOME" "$HOME"
umask 077
if [ ! -f "$ETERNALCHAT_HOME/config.json" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    printf '{\n  "connection": "subscription",\n  "cli": "%s"\n}\n' "${ETERNALCHAT_FIRST_CLI:-codex}" \
        > "$ETERNALCHAT_HOME/config.json"
fi

# Fly's proxy reaches the app on its private network, so it listens beyond
# loopback; it answers only to this app's own name and IP addresses.
set -- --no-browser --host 0.0.0.0 --port 8765
if [ -n "${FLY_APP_NAME:-}" ]; then
    set -- "$@" --allow-host "$FLY_APP_NAME.fly.dev"
fi
for name in ${ETERNALCHAT_ALLOW_HOSTS:-}; do
    set -- "$@" --allow-host "$name"
done
exec python3 -m eternalchat web "$@"
