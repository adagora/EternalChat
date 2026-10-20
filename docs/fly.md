# EternalChat on Fly.io

Your chat at `https://NAME.fly.dev`, reachable from every device, with no
computer of yours left on. The server stops a few minutes after the last page
closes and starts again in seconds on the next visit. The chat lives on a 1 GB
disk that stays when the server stops. Replies come from your Codex or Claude
Code subscription, or from an Anthropic API key.

Everything below runs from this checkout, on your Mac.

## Before you start

1. A Fly.io account with a payment card (new accounts have no free tier; see
   [what it costs](#what-it-costs)). Then:

   ```sh
   brew install flyctl
   fly auth login
   ```

2. For Codex: in ChatGPT, open Settings → Security and turn on **device code
   login**. The server has no browser, so Codex signs in with a code instead.

## 1. Deploy

```sh
deploy/fly.sh up eternalchat-yourname ams
```

The name becomes the address (`https://eternalchat-yourname.fly.dev`). The
region is the nearest Fly region: `ams` Amsterdam, `fra` Frankfurt, `waw`
Warsaw, `lhr` London, `iad` Virginia, `sjc` California (`fly platform regions`
lists them all).

The script creates the app, builds the image on Fly's builders, creates the
volume, starts one machine and prints your private link. Only `eternalchat/` and
two small scripts are uploaded, never `.env` or your local chats. Running it
again later updates EternalChat, Codex and Claude Code to their newest versions;
the chat and the sign-ins stay.

## 2. Sign in

```sh
deploy/fly.sh login codex     # shows a code: enter it at the address it prints
deploy/fly.sh login claude    # prints an address: sign in there, paste the code back
```

Sign in to the one you use, or both. The logins are kept on the volume and
refresh on their own. The server starts set to Codex. To switch, open the page
and go to **Settings → Connection**: Codex or Claude Code, the models and the
reasoning effort. The choice is saved on the server.

To use an API key instead, run `fly secrets set ANTHROPIC_API_KEY=sk-ant-…`
(the server restarts with it), then choose **API key** in Settings.

## 3. Open it everywhere

```sh
deploy/fly.sh link
```

This prints `https://NAME.fly.dev/#…`. Open it on your computer, then go to
Settings → *Your devices* and scan its QR code with your phone. To install it as
an app:

- **Android:** Chrome → ⋮ → **Install app**.
- **iPhone:** Share → **Add to Home Screen**, then paste the link once when the
  app asks for it.

The link is the key to your chat, so keep it private.

## Looking after it

| Command | What it does |
| --- | --- |
| `deploy/fly.sh doctor` | Checks every part: your Fly login, one machine, the volume, the page, the chat, the sign-ins. Prints the fix for anything that fails. |
| `deploy/fly.sh status` | Messages, summaries, failures, and this session's tokens |
| `deploy/fly.sh backup` | Downloads the whole chat as a `.tar.gz`. The copy is checked before download, so it always opens. |
| `deploy/fly.sh up` | Updates EternalChat and both CLIs |
| `fly logs` | What the server is saying |

Fly also takes daily snapshots of the volume (`fly volumes snapshots list`).

To revoke the link on every device, make a new one:
`fly ssh console -C "rm /data/eternalchat/web-token"`, then
`fly machine restart`, then `deploy/fly.sh link`.

## What it costs

- **The machine** (shared CPU, 1 GB) is billed per second while it runs. Running
  nonstop it would cost a few dollars a month. It stops a few minutes after the
  last page closes, so light use costs far less.
- **The volume:** 1 GB at $0.15 a month, plus the snapshots.
- **Replies:** your subscription, or API use billed by Anthropic.

## Good to know

- **An open page keeps the server running**, because it holds a connection to
  stream replies. Close the tab and it stops within minutes. Phones close the
  connection on their own once the app is in the background.
- **The first visit after a stop takes a few seconds** while the server starts.
  A summary that was cut short by a stop is finished at the next start.
- **Exactly one machine.** Each machine has its own volume, so a second machine
  would be a second, different chat. `doctor` checks this; never scale it up.
- **A subscription is for one person.** For other people in your home, see
  [home-server.md](home-server.md#good-to-know).
- **The chat on Fly is separate** from the one on your Mac. It starts empty.
