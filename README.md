# tiktok-automation

Reusable Python CLI for publishing one finished local video through TikTok Studio and Camoufox. Calling services own their queues and schedules. Version comes from `pyproject.toml`.

**Status: implemented, with live Camoufox acceptance outstanding.** Contract tests and a controlled local HTML page exercise the journal and browser adapter. A human-operated Studio session on 2026-10-06 established the upload route and accessible control names, and accepted an MP4 of 271.4 MB / 38 seconds / 1080 × 1920. That session is evidence about Studio; this CLI has not yet completed a real TikTok publication. Its live account navigation, selectors, upload processing, cover persistence and post confirmation must still be checked in the selected account. Linux virtual display behavior is also unverified. Do not treat the CLI as verified for unattended production publishing yet.

The [TikTok plan](https://gitlab.com/reforms-lab/root/-/blob/main/docs/publishing/tiktok.md) and [shared publishing contract](https://gitlab.com/reforms-lab/root/-/blob/main/docs/publishing/contract.md) define the interface and acceptance gates.

## Install and sign in once

Requires Python 3.12 or 3.13, `uv`, GitLab SSH access to the pinned private `metadata-automation` package, and `ffprobe` from ffmpeg. On macOS install ffmpeg through Homebrew; on Debian/Ubuntu use the distribution's ffmpeg package. Linux `--headless` uses Camoufox's virtual display and requires `Xvfb`.

```sh
uv sync --locked
uv run python -m camoufox fetch
export TIKTOK_PUBLISH_STATE_DIR="$PWD/.local/state"
uv run tiktok-publish login --account caribbean
uv run tiktok-publish accounts list
```

`login` opens a separate visible Camoufox profile. Finish the interactive login and 2FA there, then press Enter in the terminal. On macOS/Linux stdin readiness is awaited through the event loop: timeout or cancellation removes that reader and closes the session without waiting for a blocked input thread. The CLI binds the alias to the exact username read from authenticated TikTok navigation's Profile link. A display name cannot establish a binding. Re-login cannot change that binding, and another alias cannot bind to the same username. Subsequent invocations reuse the same persistent profile and the same private fingerprint JSON; no repeated login or one-off browser script is needed unless TikTok expires the session.

Dependencies are locked to Camoufox **0.5.7**, Playwright **1.60.0**, and `metadata-automation` commit `a862f19a08f84d051f8359a0a760ab394cb47d33`. The Camoufox package pairs with browser **official 156.0.1-beta.34**. The CLI checks its `version.json` before launching and rejects a different build. [Camoufox installation](https://camoufox.com/python/installation/), [persistent context options](https://camoufox.com/python/usage/), and [Linux virtual display](https://camoufox.com/python/virtual-display/) describe upstream setup. `CAMOUFOX_EXECUTABLE_PATH` is supported upstream for an explicitly installed matching executable with the browser release's `version.json` in a parent directory; isolated verification uses this to avoid changing another project's global active browser. On macOS an isolated custom installation also needs `application.ini` beside the executable, linked from `../Resources/application.ini`, so upstream can read the Firefox version.

The state directory defaults to `~/.local/share/tiktok-publish`. Set `TIKTOK_PUBLISH_STATE_DIR` to an absolute path for service calls, or pass `--state-dir PATH` after the subcommand. Keep the same directory for login, publishing, status and reconciliation regardless of the calling process's working directory.

## Publish and call from another process

```sh
uv run tiktok-publish publish \
  --account caribbean --request-id caribbean-2026-10-06-001 \
  --video /absolute/path/video.mp4 --caption-file /absolute/path/caption.txt \
  --privacy everyone --ai-generated --cover /absolute/path/cover.png

uv run tiktok-publish status --account caribbean --request-id caribbean-2026-10-06-001
```

`--privacy` is required: `everyone`, `friends`, or `only-you`. An unavailable or unreadable audience stops the command before Post. `--ai-generated` defaults to false; the script explicitly checks the toggle state and the enabled preview label. `--cover` accepts a local PNG/JPEG, uploads it through Edit cover → Upload cover image and waits for the accepted image and Save. An omitted cover uses Studio's video cover. The description is the exact UTF-8 file content, including whitespace and line breaks; if Studio normalizes it, the exact-text check stops publishing. No caption source means an empty description. `--caption-file` and `--metadata-file` are mutually exclusive.

```sh
uv run tiktok-publish publish \
  --account caribbean --request-id caribbean-2026-10-06-002 \
  --video /absolute/path/video.mp4 \
  --metadata-file /absolute/path/metadata.json --privacy everyone
```

The full metadata envelope is validated with the pinned package's bundled JSON schema and semantic checks. All nine platform blocks and `schema_version: "1.0.0"` are required. The CLI takes `platforms.tiktok.caption` verbatim; it does not append the hashtags array. Input JSON cannot override media, account, request ID or audience. No schema is copied into this repository or downloaded from an input `$ref`.

By default publishing opens a visible browser and has a total 1800-second timeout. Pass `--timeout-seconds N` for a different positive deadline. `--headless` uses headless mode on macOS and `headless="virtual"` on Linux. Use the visible mode for initial login and live acceptance.

For a service, invoke the installed entry point directly and parse its one JSON line:

```python
import json
import subprocess

call = subprocess.run(
    ["/absolute/path/tiktok-automation/.venv/bin/tiktok-publish", "publish",
     "--state-dir", "/absolute/path/tiktok-automation/.local/state",
     "--account", "caribbean", "--request-id", "job-123",
     "--video", "/absolute/path/video.mp4", "--privacy", "everyone"],
    capture_output=True, text=True,
)
result = json.loads(call.stdout)
```

Every command, including invalid arguments and `--help`, emits one JSON object on one stdout line. Help text is the JSON `help` field. Diagnostics go to stderr. Publishing and status results contain `platform`, `account`, `request_id`, `status`, `post_url` and `error` (null or `{code, message}`). Login returns `authenticated`; account listing returns `ok` with saved aliases, usernames and canonical profile URLs, without session data.

| Exit | Publish / reconcile result |
| --- | --- |
| 0 | Confirmed or cached `published`. |
| 2 | Invalid arguments/media/metadata, account mismatch, login required, unavailable audience, or request conflict before Post. |
| 3 | Busy profile, browser/tooling/UI failure before Post. |
| 4 | `uncertain` after the submission boundary; no automatic repost. |

`status` returns 0 for any found journal record, including `uncertain`, and 2 for an invalid/missing account/request pair. Successful login and account listing return 0; their input/login errors return 2 and technical errors return 3.

## Journal, confirmation and reconciliation

An account lock covers the entire browser session and journal update. A concurrent invocation fails immediately with `PROFILE_BUSY`; there is no internal queue. Before opening the browser, the CLI copies the readable regular video and optional cover into a request directory, validates the video with ffprobe, hashes the copies and uploads only those snapshots. Source changes cannot alter the bytes being uploaded. Actual formats, duration, dimensions and size limits are decided by the current Studio editor; local checks do not invent TikTok limits or transcode the file.

The SQLite key is `(account, request_id)`. Its fingerprint includes the original absolute paths, exact caption, SHA-256 of each copied file, privacy, AI label and cover choice. A request ID with changed arguments is rejected before opening a browser. After a pre-submit failure, a retry additionally rechecks caption and media bytes. A saved `published` or `uncertain` result returns without reading media, opening a browser or clicking Post; the original arguments are compared with the record, and the saved caption/media snapshot remains authoritative. Reusing the same filename after changing its content does not create a new post with an already terminal ID.

Immediately before the one Post click, the CLI rechecks the exact account, caption, audience, AI label, saved-cover step and Now radio. It atomically commits `submitting` first. A timeout, crash, closed browser or failed confirmation thereafter produces `uncertain` (exit 4). A crash leaving `submitting` is recovered as `uncertain`; `status` reports that interpretation without mutating the record. No retry path clicks Post again.

Confirmation requires a personal View post/View video link in this editor's Post success dialog, first appearing after the single click. The CLI then verifies that link's exact video ID, account username and full caption from structured item data, waits for that exact URL in the asynchronously loaded Posts list, and reads its persisted audience. A matching caption or a new entry in the general Posts list cannot identify the uploaded bytes: another client could concurrently publish the same caption. This implementation therefore never chooses an ID by comparing those lists.

The success-dialog message/link adapter is verified only on controlled HTML; that dialog and its selectors have not been observed in live Camoufox. The human-operated Studio session on 2026-10-06 redirected directly to the general Posts list. If the live editor follows that route without exposing the personal success link, this CLI returns `uncertain`, `post_url: null`, and `CAUSAL_POST_ID_MISSING`. A live acceptance pass must identify a reliable current-Post response or personal success UI before this confirmation can be considered production-ready.

Studio was also observed to initially list a requested Everyone post as Only me. The CLI does not change any submitted post's audience. A mismatch remains `uncertain` for explicit inspection and correction of that identified post by the operator. For Everyone, successful confirmation additionally checks an unauthenticated fetch for the exact post, `privateItem: false` and a playable URL.

`Content under review` remains `uncertain` with `post_url` only when that ID came from this Post's success link and its author/caption were verified. After TikTok completes review or an operator corrects the known post's audience, explicitly check this saved post with:

```sh
uv run tiktok-publish reconcile \
  --account caribbean --request-id caribbean-2026-10-06-001
```

`reconcile` checks only the saved URL, author, ID, caption, audience and absence of review. It does not upload, click Post or change privacy. A successful check changes the journal to `published`; otherwise it keeps `uncertain`.

When the original result has `post_url: null`, manually find the specific post and compare its actual video with the original request's video or private snapshot. A matching caption alone is insufficient. After that comparison, explicitly supply both options:

```sh
uv run tiktok-publish reconcile \
  --account caribbean --request-id caribbean-2026-10-06-001 \
  --post-url https://www.tiktok.com/@caribbeanawesome/video/EXACT_ID \
  --confirm-request-video
```

`--confirm-request-video` is the operator's assertion that they compared the actual video. The adapter independently checks that exact ID's author, full caption, persisted audience and absence of review; Everyone additionally requires guest playback evidence. The supplied URL is saved only after all checks pass. A mismatch or review keeps `uncertain` and the previous URL unchanged. A saved post binding cannot be replaced by a different URL. The CLI cannot independently compare uploaded/transcoded video bytes, so this manual assertion is an explicit human step, not inferred proof. Preserve the journal and diagnostics and do not invent another request ID to bypass uncertainty.

State directories have mode 0700 and private files 0600. Profiles, fingerprints, SQLite (including WAL/sidecars), snapshots and screenshots belong in `.local/` and are excluded from Git. Captions, cookies, passwords, storage state and full DOM are not logged. Screenshots may contain private information. After a terminal request is resolved, delete only its copied media and `diagnostics/` if desired; retain the SQLite journal and account profile for idempotency. Remove the whole state directory only when intentionally abandoning all stored bindings and duplicate protection. Keep backups of the journal before maintenance.

## Verification

```sh
uv run pytest tests/test_contract.py
uv run pytest tests/test_browser.py
```

The controlled browser tests use a new disposable Camoufox profile and local HTML; they make no real TikTok post. These checks cover one-line JSON, strict metadata, byte snapshots, interprocess locks, alias binding, request conflicts including AI/cover settings, failed retry, cached results without media, uncertainty, crash recovery, cancellable login input and no second Post. Controlled DOM checks cover caption preservation, audience reads, AI toggle, causal success-link confirmation, a competing identical-caption post, and delayed loading of the exact known post ID. Live Studio acceptance and the Linux runtime remain separate gates.
