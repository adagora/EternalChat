#!/bin/sh
# Runs inside the EternalChat machine; deploy/fly.sh calls it through
# `fly ssh console`, whose sessions do not carry the image's environment.
set -eu
export ETERNALCHAT_HOME=/data/eternalchat HOME=/data/home PYTHONPATH=/app
BACKUP=/tmp/eternalchat-backup.tar.gz

# The running page's state, as the page reads it: while it serves the chat, no
# other process may open it, not even to read.
state() {
    python3 -c '
import json, urllib.request
token = open("/data/eternalchat/web-token").read().strip()
request = urllib.request.Request("http://127.0.0.1:8765/api/state", headers={"X-EternalChat-Token": token})
print(json.dumps(json.load(urllib.request.urlopen(request, timeout=20)), indent=2))'
}

case "${1:-}" in
    token)
        cat "$ETERNALCHAT_HOME/web-token" ;;
    login-codex)
        exec codex login --device-auth ;;
    login-claude)
        exec claude auth login ;;
    status)
        state ;;
    doctor)
        # The machine's half of `deploy/fly.sh doctor`: the chat opens, and
        # the connection it is set to can reach a model.
        problems=0
        ok() { printf '  ✔ %s\n' "$1"; }
        bad() { printf '  ✗ %s\n    Fix: %s\n' "$1" "$2"; problems=$((problems + 1)); }
        config="$ETERNALCHAT_HOME/config.json"
        connection=$(sed -n 's/.*"connection": *"\([a-z]*\)".*/\1/p' "$config" 2> /dev/null)
        cli=$(sed -n 's/.*"cli": *"\([a-z]*\)".*/\1/p' "$config" 2> /dev/null)
        if counts=$(state 2> /dev/null); then
            ok "The chat is served: $(printf '%s' "$counts" | python3 -c 'import json,sys; s=json.load(sys.stdin)["status"]; print(s["messages"], "messages,", s["failed"], "failed summaries")')"
        else
            bad "The chat is not served" "fly logs shows why; restore a backup if the chat is damaged"
        fi
        case "${connection:-api}" in
            subscription) ok "Connection: ${cli:-codex} subscription" ;;
            *) ok "Connection: API (its key is the Fly secret ANTHROPIC_API_KEY)" ;;
        esac
        if codex login status 2>&1 | grep -q "Logged in"; then
            ok "Codex is signed in"
        elif [ "$connection" = subscription ] && [ "${cli:-codex}" = codex ]; then
            bad "Codex is not signed in, and the chat uses it" "deploy/fly.sh login codex"
        else
            ok "Codex is not signed in (not in use)"
        fi
        if claude auth status 2>&1 | grep -q '"loggedIn": *true'; then
            ok "Claude Code is signed in"
        elif [ "$connection" = subscription ] && [ "$cli" = claudecode ]; then
            bad "Claude Code is not signed in, and the chat uses it" "deploy/fly.sh login claude"
        else
            ok "Claude Code is not signed in (not in use)"
        fi
        used=$(df -h /data | awk 'NR == 2 { print $3 " of " $2 " used" }')
        ok "Disk: ${used:-unknown}"
        exit "$problems" ;;
    backup)
        # A copy taken while the chat writes may end mid-record. Each copy is
        # opened read-only, which checks every record, and taken again until
        # one passes: what is downloaded always opens.
        copy=/tmp/eternalchat-copy
        rm -rf "$copy" "$BACKUP"
        for attempt in 1 2 3 4 5; do
            mkdir -p "$copy"
            cp -a "$ETERNALCHAT_HOME" "$copy/"
            if python3 -m eternalchat --data-dir "$copy/eternalchat/chat" status > /dev/null 2>&1; then
                tar -C "$copy" -czf "$BACKUP" eternalchat
                rm -rf "$copy"
                echo "$BACKUP"
                exit 0
            fi
            rm -rf "$copy"
            sleep 2
        done
        echo "The chat kept changing while it was copied; try again in a moment." >&2
        exit 1 ;;
    *)
        echo "usage: machine.sh token | login-codex | login-claude | status | doctor | backup" >&2
        exit 2 ;;
esac
