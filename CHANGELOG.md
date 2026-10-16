# Changelog

All notable changes to this project are documented here. The format follows Keep a Changelog, and versions follow Semantic Versioning.

## [Unreleased]

### Fixed

- Enter the caption through real input instead of dispatching a fabricated `paste` event into the editor. The old path built an `Event("paste")` carrying a plain object in place of a `DataTransfer`; a page sees such an event as `isTrusted: false`, so every publish marked itself as automated.

### Added

- `human` module for input that leaves the trace a person leaves: an eased approach to a control, a pause before pressing, typing with a varying interval, and a real clipboard paste. Measured against the alternatives, `fill` delivers 49 characters with no keystrokes, `execCommand("insertText")` the same without even a `beforeinput`, and `type(delay=55)` leaves 0–3 ms between keystrokes against this module's 42–186 ms.
- `human.enter_text` types the ASCII runs of a text and inserts the rest, which is what an emoji picker produces for a person: a typed emoji arrives with no keystroke behind it anyway. It does not touch the system clipboard. A real clipboard paste is a closer imitation for a long caption and stays available behind `allow_clipboard`, but it is off by default: the clipboard is shared with the person at the machine and with every other agent on it, so between writing our text and pressing the paste key, someone else's copied content — a password among the possibilities — can be what lands in the field, and a draft editor saves what it holds. On that path the clipboard is re-checked immediately before the key, the text is read back from the control afterwards, and a control that did not receive it is cleared and typed into.
- Test that fails the build if any module dispatches a browser event again.

## [0.2.1] - 2026-10-08

### Fixed

- Wait for a large upload to enable Post before treating the pre-submit check as failed.
- Scope the persisted audience to the live post row containing exactly the known video link, and reject an unrelated audience control below it.
- Use the single item ID from the observed response to this Post's publish request when Studio redirects without a personal success dialog; independently verify the exact post, caption, audience, review state, and guest playback. Save only sanitized response diagnostics.

## [0.2.0] - 2026-10-07

### Added

- Import only live, unpartitioned TikTok cookies from a read-only Firefox SQLite snapshot into a fresh persistent Camoufox profile, then verify the explicitly requested username before binding it.
- Visible `accept-ui` rehearsal with private media snapshots and the same account, caption, audience, AI and cover checks as publishing, without Post or a request journal entry.
- Cookie-free Camoufox fallback for public post verification when an unauthenticated HTTP request receives TikTok's JavaScript challenge.

### Fixed

- Keep waiting for this Post's personal success link when Studio redirects before displaying the success dialog; preserve uncertain results when no causal link is available.
- Preserve host-only cookie and path scope when importing Firefox cookies through Playwright.
- Support observed TikTok Profile, Draft description, cover, audience and AI controls, dismiss editor onboarding, and handle the one-time AI confirmation.
- Enter Draft captions through the editor's paste event and read their exact plain-text blocks so later editor renders cannot restore the filename or lose characters.
- Allow only space/tab/newline rendering changes when verifying published captions, while preserving the exact caption check in the editor.
- Reject audience controls from a container that also contains another post ID.

## [0.1.1] - 2026-10-06

### Fixed

- Pin metadata-automation `0.2.0` to accept complete schema `2.0.0` envelopes with ten platforms, including Pinterest, while preserving `1.0.0` support and exact TikTok captions.

## [0.1.0] - 2026-10-06

### Added

- Reusable `tiktok-publish` CLI with visible login, persistent account profiles, account listing, video publishing, local status and explicit read-only post reconciliation.
- Explicit manual reconciliation of an uncertain request without a saved URL using `--post-url` and `--confirm-request-video`, followed by independent identity/visibility checks.
- Exact caption/full pinned metadata validation, required audience, optional AI-generated label and cover, private immutable media snapshots and process locks.
- SQLite idempotency journal with a durable one-click submission boundary, crash recovery and explicit uncertain results for review or unconfirmed visibility.
- Conservative TikTok Studio adapter using observed accessible control names, exact post identity checks and persisted audience confirmation.
- Python 3.12 packaging, pinned dependencies, lockfile and controlled contract/browser checks. Live Camoufox publication acceptance remains outstanding.

### Fixed

- Require a personal success link causally connected to the current Post; never infer an uploaded item from list differences or a matching caption, and never mutate a submitted post's privacy.
- Wait for the exact known post URL after asynchronous Posts loading during confirmation and reconciliation.
- Make manual-login stdin waiting cancellable on macOS/Linux so a timeout cannot wait on a blocked input thread.
