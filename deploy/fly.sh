#!/bin/sh
# EternalChat on Fly.io, from this checkout (docs/fly.md):
#
#   deploy/fly.sh up NAME [REGION]   create the app the first time, and deploy; again to update
#   deploy/fly.sh login codex        sign Codex in with your ChatGPT account (device code)
#   deploy/fly.sh login claude       sign Claude Code in with your Claude account
#   deploy/fly.sh link               print the chat's private link
#   deploy/fly.sh status             messages, summaries and this session's tokens
#   deploy/fly.sh doctor             check every part, with the fix for what fails
#   deploy/fly.sh backup [FILE]      download the whole chat as a .tar.gz
#
# NAME becomes https://NAME.fly.dev and is kept in fly.toml, so the other
# commands need no name. REGION is a Fly region code, e.g. ams, fra, lhr, iad.
set -eu
cd "$(dirname "$0")/.."
case "${1:-}" in
    up|login|link|status|doctor|backup) ;;
    *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
FLY=$(command -v fly || command -v flyctl || true)
if [ -z "$FLY" ]; then
    echo "Install flyctl first: brew install flyctl (or https://fly.io/docs/flyctl/install/)" >&2
    exit 1
fi

app() { sed -n 's/^app = "\(.*\)"$/\1/p' fly.toml; }
url() { echo "https://$(app).fly.dev"; }
on() { "$FLY" ssh console --app "$(app)" "$@"; }

# A stopped machine starts on its next request; ssh needs it started.
wake() { curl -fsS -o /dev/null --max-time 90 "$(url)/manifest.webmanifest" || true; }

token() { on -q -C "/app/deploy/machine.sh token" | tr -d '\r\n'; }

# Fly stops a machine nobody is using, and an ssh session does not count. A
# sign-in can take minutes, so an open event stream keeps it running meanwhile.
hold() {
    header=$(mktemp)
    printf 'X-EternalChat-Token: %s\n' "$(token)" > "$header"
    curl -sN -H @"$header" "$(url)/api/events" > /dev/null 2>&1 &
    held=$!
    trap 'kill $held 2> /dev/null; rm -f "$header"' EXIT INT TERM
}

case "${1:-}" in
    up)
        name=${2:-$(app)}
        case "$name" in
            ""|*[!a-z0-9-]*) echo "Give an app name of lowercase letters, digits and dashes: deploy/fly.sh up NAME" >&2; exit 2 ;;
        esac
        "$FLY" auth whoami > /dev/null 2>&1 || "$FLY" auth login
        if ! "$FLY" status --app "$name" > /dev/null 2>&1; then
            "$FLY" apps create "$name"
        fi
        sed -i.bak "s/^app = .*/app = \"$name\"/" fly.toml
        if [ -n "${3:-}" ]; then
            sed -i.bak "s/^primary_region = .*/primary_region = \"$3\"/" fly.toml
        fi
        rm -f fly.toml.bak
        "$FLY" deploy --ha=false --volume-initial-size 1 --yes
        echo
        echo "Your chat: $(url)/#$(token)"
        echo "Keep this link private. Next: deploy/fly.sh login codex (or claude)."
        ;;
    login)
        case "${2:-}" in
            codex|claude) ;;
            *) echo "usage: deploy/fly.sh login codex|claude" >&2; exit 2 ;;
        esac
        wake
        hold
        on --pty -C "/app/deploy/machine.sh login-$2"
        ;;
    link)
        wake
        echo "$(url)/#$(token)"
        ;;
    status)
        wake
        on -q -C "/app/deploy/machine.sh status"
        ;;
    doctor)
        problems=0
        ok() { printf '  ✔ %s\n' "$1"; }
        bad() { printf '  ✗ %s\n    Fix: %s\n' "$1" "$2"; problems=$((problems + 1)); }
        count() { python3 -c 'import json,sys; print(len(json.load(sys.stdin) or []))' 2> /dev/null || echo 0; }
        name=$(app)
        echo "EternalChat on Fly.io: $name"
        ok "flyctl $("$FLY" version 2> /dev/null | awk '{ print $2 }')"
        if who=$("$FLY" auth whoami 2> /dev/null); then ok "Signed in to Fly as $who"
        else bad "Not signed in to Fly" "fly auth login"; fi
        if "$FLY" status --app "$name" > /dev/null 2>&1; then
            ok "App $name exists"
            machines=$("$FLY" machine list --app "$name" --json 2> /dev/null | count)
            case "$machines" in
                1) ok "One machine" ;;
                0) bad "No machine" "deploy/fly.sh up" ;;
                *) bad "$machines machines: each holds a different chat" "fly scale count 1 --app $name (keep the one with your chat)" ;;
            esac
            volumes=$("$FLY" volumes list --app "$name" --json 2> /dev/null | count)
            if [ "$volumes" -ge 1 ]; then ok "Volume for the chat"; else bad "No volume" "deploy/fly.sh up"; fi
            if curl -fsS -o /dev/null --max-time 90 "$(url)/manifest.webmanifest"; then
                ok "The page answers at $(url)"
                on -q -C "/app/deploy/machine.sh doctor" || problems=$((problems + $?))
            else
                bad "The page does not answer at $(url)" "fly logs --app $name shows why; then deploy/fly.sh up"
            fi
        else
            bad "No app named $name" "deploy/fly.sh up $name"
        fi
        if [ "$problems" -eq 0 ]; then echo "All good."; else echo "$problems to fix."; fi
        exit "$((problems > 0))"
        ;;
    backup)
        file=${2:-eternalchat-$(date +%Y%m%d-%H%M).tar.gz}
        wake
        on -q -C "/app/deploy/machine.sh backup" > /dev/null
        "$FLY" ssh sftp get --app "$(app)" /tmp/eternalchat-backup.tar.gz "$file"
        echo "Saved $file"
        ;;
esac
