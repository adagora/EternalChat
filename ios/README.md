# EternalChat iOS companion

This optional prototype is outside the required project scope at the user's
request. Xcode, native signing and APNs device acceptance are not release gates
for EternalChat; use the browser/PWA described in [mobile setup](../docs/mobile.md).

Our SwiftUI/WKWebView client pairs with an EternalChat HTTPS server and stores
its own session in Keychain. It includes native APNs opt-in, origin-bound
navigation and credential injection, microphone access, memory ZIP export through the system share sheet, and device revocation.
There is no share extension, as requested. Chat uses our own page and backend.

Source parsing and shared Foundation connection checks passed on this Mac.
**The UIKit app has not been compiled, signed, installed or phone-tested.** Full
Xcode is absent. A successful Xcode build and signed-device acceptance remain
required; source parsing is not a substitute.

Install full Xcode and XcodeGen. Run `./build.sh` to generate a project and build
an unsigned simulator app. It does not publish or upload anything. Open the
resulting `EternalChat.xcodeproj` in Xcode to run it. Set your own bundle ID in
`project.json`, select your signing team and enable Push Notifications for that
app ID. `org.eternalchat.companion` is a placeholder, not a provisioned app ID.

Create a pairing link in a connected browser's Settings and paste it into the
app. Links work once and expire after ten minutes. The server needs an HTTPS
certificate trusted by iOS, such as your Tailscale HTTPS address. Model keys
and subscription logins stay on the server.

For native push, configure the server:

- `ETERNALCHAT_APNS_KEY`: private `.p8` file path, outside chat memory.
- `ETERNALCHAT_APNS_TEAM`: your Apple team ID.
- `ETERNALCHAT_APNS_KEY_ID`: APNs authentication key ID.
- `ETERNALCHAT_APNS_TOPIC`: the exact signed bundle ID.
- `ETERNALCHAT_APNS_SANDBOX=1` for development/Debug devices, `0` for production.

The server requires `eternalchat[push]` and curl with HTTP/2 support. Its APNs
endpoint, topic and environment are server-owned. Authentication uses ES256;
alerts contain only “A reply is ready.” Then choose Enable notifications from
the app menu. Apple developer signing/provisioning is required.

On a signed physical device, verify chat/images/import/export/recording, closed-app delivery, alert tap, renewed
device tokens, disable and revocation. JWT/request fixtures do not establish
Apple delivery. Debug and production tokens cannot be interchanged.

Shared-code checks without full Xcode:

```sh
swiftc -O -module-cache-path /tmp/eternalchat-swift-cache \
  ios/Shared/Connection.swift tests/ios_connection.swift -o /tmp/eternalchat-ios-check
/tmp/eternalchat-ios-check
swiftc -parse ios/Shared/Connection.swift ios/App/Session.swift ios/App/EternalChat.swift
```

The 1024px RGB icon comes from our existing `scripts/make_icons.py` geometry
using `png(1024, radius=0, scale=.86, opaque=True)`.
