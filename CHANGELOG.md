# Changelog

All notable changes to this project are documented here. The format follows Keep a Changelog, and versions follow Semantic Versioning.

## [Unreleased]

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
