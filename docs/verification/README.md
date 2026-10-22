# Verification records

The in-scope parity implementation is complete. Native iOS, Xcode, app signing
and the share extension are excluded at the user's request. Mobile support uses
the browser/PWA. The implementation plan has been removed; historical native
results in the JSON records do not create current release requirements.

The latest full run passed 283 tests with optional push dependencies installed.
Restart/recall smoke passed. Chromium/WebKit passed 12 offline UI flows; new
session/push screens were reviewed at phone widths. See
[mobile implementation evidence](mobile-parity-2026-10-22.json),
[browser acceptance](mobile-browser-2026-10-22.json), and
[API configuration checks](api-config-browser-2026-10-22.json).

Live verification remains distinct from implementation:

- Browser push and transcription: credentials/configuration and real-device
  delivery/speech checks remain outstanding (`ec-cuo`). See [setup](../mobile.md).
- macOS screen actions: successful native capture/actions need Screen Recording
  permission (`ec-4y8`). The adapter compiled and offline isolation/routing passed.
- Subscription acceptance/comparison: real Codex workflows passed; Claude's live
  baseline was quota-blocked (`ec-rq1`). See the [baseline](subscription-baseline-2026-10-21.json)
  and [Codex follow-up](codex-acceptance-followup-2026-10-21.json). No subscription
  savings or actual-money comparison is established.

These records preserve the limits of the checks; unverified live behavior is not
reported as passed. Existing issues track those checks without a separate parity
implementation plan.
