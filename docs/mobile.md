# Devices and background notifications

Each browser exchanges the server's bootstrap link for its own 90-day session.
The bootstrap token stays on the server; browsers save only their own credential.
Settings lists paired browsers and computer transports. Revoke ends that session's
connections and rejects new requests. Forget this device revokes it before clearing
local data. Work accepted before revocation is not rolled back.

**Create pairing link** creates a single-use link that expires after ten minutes.
Copy it or scan its QR code on your other device. An installed iOS Home Screen app
has separate browser storage, so pair it separately. Pairing links do not contain
the current browser's session. Opening the server's original terminal link can
always create another session, so keep that administrator link private.

Computer Settings → **Create command** creates an independent computer-only
credential for `eternalchat device`. It binds to the first joined computer name;
it cannot read chat history, send messages, change settings or target another
name. Its enrollment identity and receipt journal still protect reconnects and
uncertain side effects. Revoke its credential from Your devices. Legacy clients
and terminals using the original administrator token retain full access; stop
using copied administrator links for normal device pairing. To invalidate the
administrator token and all derived sessions, stop the server, remove its
`~/.eternalchat/web-token`, and restart. You must then pair devices again.

Saved credential hashes and push subscriptions live in a private `web-auth.json`
inside the chat folder. Memory ZIP exports exclude it. Restoring a memory backup
therefore requires fresh pairing. Bootstrap rotation invalidates derived sessions.
Private VAPID/APNs keys belong outside the chat folder and stay out of backups.

## Browser push setup

The chat itself remains dependency-free. Background push needs the optional
maintained encryption libraries and curl. In your Python environment:

```sh
python3 -m pip install '.[push]'
python3 -m eternalchat.push "$HOME/.eternalchat/push-key.pem"
export ETERNALCHAT_PUSH_KEY="$HOME/.eternalchat/push-key.pem"
export ETERNALCHAT_PUSH_SUBJECT='mailto:you@example.com'
eternalchat web --no-browser
```

Create the parent directory first if needed. Key generation refuses to overwrite
an existing identity and writes it with mode 0600. Use your contact address for
the subject. Keep the key stable, and pass these environment variables to your
LaunchAgent/container as well. Restart the server after changing its key or
configuration. The container includes the optional push dependencies; the key
must live on a persistent volume, outside the chat folder.

Open the server over trusted HTTPS, install it if needed, then enable **When a
reply arrives** in Settings. Permission is requested only after your action.
Use **Send test notification**, close the page, and send a message from another
paired device. On iPhone/iPad use the Home Screen app (iOS/iPadOS 16.4 or later).
No Apple developer membership is required for standards-based browser Web Push.
See [WebKit's platform documentation](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/).

The server encrypts a generic “A reply is ready” alert for each subscribed session
when a logical turn completes. Chat text, credentials and private links are absent.
Chrome/FCM, Firefox/Mozilla and Safari/Apple endpoints are accepted; other endpoint
hosts are rejected. Payload encryption uses aes128gcm and VAPID authentication via
[pywebpush](https://github.com/web-push-libs/pywebpush). Network requests use verified
TLS, refuse redirects, time out, and are cancelled on shutdown. The queue coalesces
old alerts; failed sends are not retried automatically. Expired endpoints are
removed. Delivery still depends on the browser, push service and OS.

Disabling notifications unregisters the server subscription and browser endpoint.
Renew a changed/expired browser subscription by turning it off and on. A changed
VAPID key requires a renewed subscription. Revocation removes the subscription;
an alert already accepted by a push service can still arrive during its five-minute
TTL. If push is unavailable or unconfigured, the existing page-only notification
mode remains available and is labelled accordingly.

## Mobile scope

The supported mobile path is the browser/PWA. Native iOS, Xcode builds, app
signing and the share extension are outside the required scope. Browser Web Push
on iPhone/iPad uses the Home Screen app and does not require Xcode or an Apple
developer account. The optional native prototype remains in `ios/` for reference.

## Verification boundaries

Session HTTP/SSE and restart tests use real local servers. Push transport tests
use private HTTPS fixtures, decrypt the actual payload and verify the VAPID
signature; native tests verify the APNs ES256 JWT and request shape without
contacting Apple. Browser permission, PushManager and audio responses are explicit
offline fixtures. The service-worker handler is also executed with push fixtures.
These checks do not establish vendor delivery or physical-phone behavior.
Live transcription still needs the server's OpenAI key; browser push needs its
VAPID configuration and a real-device delivery check. Native build/APNs checks
are not required for the supported browser/PWA scope.

See the [verification records](verification/README.md) for evidence and outstanding
live checks. The implementation parity plan has been removed.
