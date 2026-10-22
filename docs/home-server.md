# EternalChat at home: every phone, tablet and computer

`eternalchat web` serves the chat to this computer only. This guide puts it on
one always-on machine and reaches it from every device you own, as an
installable app, without opening anything to the internet.

## The recommended setup

```
 phone / tablet / laptop ──(Tailscale, encrypted)──▶ home machine
   https://HOST.TAILNET.ts.net                       tailscale serve :443 ─▶ 127.0.0.1:8765
   installed as an app                               eternalchat web (always running)
```

- **Tailscale** joins your devices into a private network that works at home
  and away. Nothing is exposed to the internet and no router changes are needed.
- **`tailscale serve`** gives the chat a real HTTPS address with a valid
  certificate. Phones need HTTPS before they'll install the page as an app or
  allow dictation and notifications.
- **EternalChat stays bound to 127.0.0.1.** Only Tailscale's proxy reaches it,
  and `--allow-host` names the one address it answers to.

### 1. Pick the machine that stays on

The chat lives on one machine; every device is a window onto it. That machine
must stay awake.

| Host | Fit |
| --- | --- |
| **Mac mini** (any Apple-silicon model) | Best: silent, about 5 W idle, runs the `claude` and `codex` CLIs for subscription mode, and backs up with Time Machine. |
| Linux mini PC (Intel N100 class) or Raspberry Pi 5 | Good and cheap. EternalChat needs only Python 3.11; the CLIs run on Linux too. |
| Your laptop | Fine for trying it out, but the chat goes offline whenever the lid closes or the laptop leaves home. |

On a Mac host, open System Settings → Energy and turn on **Prevent automatic
sleeping when the display is off** and **Start up automatically after a
power failure**.

### 2. Tailscale on every device

1. Install Tailscale on the host and on each phone, tablet and laptop, and sign
   in to the same tailnet. Family members can use their own accounts: share the
   host machine with them from the admin console.
2. In the admin console (login.tailscale.com → DNS), turn on **MagicDNS** and
   **HTTPS Certificates**.
3. On the host, run `tailscale status`. The first line shows the host's name,
   for example `mac-mini.tail1234.ts.net`. This guide calls it `HOST.TAILNET.ts.net`.

### 3. Serve it

On the host:

```sh
tailscale serve --bg 8765            # https://HOST.TAILNET.ts.net → 127.0.0.1:8765, kept across restarts
eternalchat web --no-browser --allow-host HOST.TAILNET.ts.net
```

`eternalchat web` prints the private link: `https://HOST.TAILNET.ts.net/#…`.
Then `tailscale serve status` shows the proxy, and `tailscale serve reset`
removes it.

### 4. Connect each device once

- **From a computer:** open the private link. In the page, open Settings →
  *Your devices* to see a **QR code** of the link.
- **Android:** scan the QR code with the camera, open the link in Chrome, then
  ⋮ → **Install app**. The installed app shares Chrome's storage, so it is
  already connected. Text, links and pages shared to EternalChat from other apps
  land in its message box.
- **iPhone / iPad:** scan the QR code, open the link in Safari, then Share →
  **Add to Home Screen**. A Home Screen app keeps its own storage, so the first
  time it opens it asks for the link: copy the link in Safari (or on your Mac,
  through Universal Clipboard) and paste it there.

Each device keeps the link until you use Settings → **Forget this device**.

### 5. Keep it running (macOS LaunchAgent)

Save this file as `~/Library/LaunchAgents/com.eternalchat.web.plist`. Replace
`YOU` with your user name and `HOST.TAILNET.ts.net` with your host's name, and
keep the `PATH` line if you use a Claude Code or Codex subscription: launchd
starts programs with a minimal PATH, which won't find the CLIs.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.eternalchat.web</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.local/bin/eternalchat</string>
    <string>web</string>
    <string>--no-browser</string>
    <string>--allow-host</string>
    <string>HOST.TAILNET.ts.net</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/Users/YOU/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/eternalchat.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/eternalchat.log</string>
</dict>
</plist>
```

```sh
touch ~/Library/Logs/eternalchat.log && chmod 600 ~/Library/Logs/eternalchat.log   # it records the private link
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.eternalchat.web.plist   # start, and at every login
launchctl kickstart -k gui/$(id -u)/com.eternalchat.web                             # restart after an upgrade
launchctl bootout gui/$(id -u)/com.eternalchat.web                                  # stop
```

With the API connection, the service needs `ANTHROPIC_API_KEY`. Either add it
under `EnvironmentVariables` and protect the file with
`chmod 600 ~/Library/LaunchAgents/com.eternalchat.web.plist`, or use a Claude
Code or Codex subscription, whose CLI keeps its own login. Run
`eternalchat setup` on the host first, so the service uses your saved choices.

On Linux, a systemd user unit does the same:
`ExecStart=%h/.local/bin/eternalchat web --no-browser --allow-host HOST.TAILNET.ts.net`
with `Restart=always`, plus `loginctl enable-linger $USER` so it runs without a
login session.

## Good to know

- **One writer.** While the service runs, it owns the chat. Use the page from
  every device, including the host itself. `eternalchat status`, `view` and
  `zoom` still work alongside it. To chat in the terminal, stop the service first.
- **The link is the key.** Anyone holding it can use your chat, so show the QR
  code only to your own devices. To revoke every device at once, delete
  `~/.eternalchat/web-token` and restart the service. A new link is printed,
  and each device must connect again.
- **Notifications** work while the page or app is open, even in the
  background. A phone that has fully suspended the app shows the reply the next
  time it opens.
- **Backups.** The whole chat is `~/.eternalchat/chat` on the host. Time
  Machine, or any file backup, covers it.

## The quick alternative: your home network only

```sh
eternalchat web --host 0.0.0.0     # then open http://NAME.local:8765/#… on another device
```

This works at once, with no extra software, and answers to the machine's name
and any of its IP addresses. It has real limits, though:

- The connection is plain HTTP, so the link's secret crosses your Wi-Fi
  unencrypted. Use it only on a network you trust.
- Phones won't install the page as an app, and dictation and notifications
  don't work.
- It only works at home.

Never forward the port on your router to the internet. If you need access from
outside, Tailscale is the safe way.

## No machine to leave on?

Run it on Fly.io instead: [fly.md](fly.md). It stops when unused, so you pay for
minutes, not months.
