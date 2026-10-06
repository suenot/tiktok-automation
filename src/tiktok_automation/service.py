import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import urlparse

from .browser import POST_RE, TikTokStudio, browser_session, current_handle, verify_account
from .errors import PublishError
from .firefox_cookies import firefox_cookies
from .media import copy_file, probe_cover, probe_video, read_caption
from .state import State, canonical, result


def normalized(args):
    return {"type": "video", "video": str(Path(args.video).expanduser().absolute()),
            "caption_file": str(Path(args.caption_file).expanduser().absolute()) if args.caption_file else None,
            "metadata_file": str(Path(args.metadata_file).expanduser().absolute()) if args.metadata_file else None,
            "privacy": args.privacy, "ai_generated": args.ai_generated,
            "cover": str(Path(args.cover).expanduser().absolute()) if args.cover else None}


def cached_exit(row):
    return 0 if row["status"] == "published" else 4


@asynccontextmanager
async def capture_diagnostics(adapter):
    try:
        yield
    except Exception as exc:
        screenshot = await adapter.screenshot()
        if screenshot:
            error = exc if isinstance(exc, PublishError) else PublishError("BROWSER_FAILURE", "Browser operation failed.", 3)
            raise PublishError(error.code, f"{error.message} Screenshot: {screenshot}", error.exit_code) from exc
        raise


async def publish(state: State, args, session_factory=browser_session, adapter_factory=TikTokStudio):
    with state.lock(args.account):
        account = state.account(args.account)
        arguments = normalized(args)
        row = state.get(args.account, args.request_id)
        if row:
            if row["arguments"] != canonical(arguments):
                raise PublishError("REQUEST_CONFLICT", "Request ID was already used with different arguments.")
            row = state.recover(row)
            if row["status"] in {"published", "uncertain"}:
                return result(row), cached_exit(row)
        directory = state.request_dir(args.account, args.request_id)
        video = directory / ("video" + Path(arguments["video"]).suffix.lower())
        video_hash = copy_file(arguments["video"], video)
        probe_video(video)
        cover, cover_hash = None, None
        if arguments["cover"]:
            cover = directory / ("cover" + Path(arguments["cover"]).suffix.lower())
            cover_hash = copy_file(arguments["cover"], cover)
            probe_cover(cover)
        caption = read_caption(arguments["caption_file"], arguments["metadata_file"])
        details = {**arguments, "caption": caption, "video_sha256": video_hash, "cover_sha256": cover_hash}
        fingerprint = hashlib.sha256(canonical(details).encode()).hexdigest()
        if row and row["fingerprint"] != fingerprint:
            raise PublishError("REQUEST_CONFLICT", "Request ID was already used with different caption or media bytes.")
        state.start(args.account, args.request_id, arguments, fingerprint, details)
        adapter, submitted = None, False
        try:
            async with asyncio.timeout(args.timeout_seconds):
                async with session_factory(state.profile(args.account), args.headless) as context:
                    adapter = adapter_factory(context, directory / "diagnostics")
                    async with capture_diagnostics(adapter):
                        await adapter.open_editor(account["handle"])
                        await adapter.prepare(video, caption, args.privacy, args.ai_generated, cover)
                        await adapter.verify(account["handle"], caption, args.privacy, args.ai_generated, cover)
                        # FULL synchronous transaction commits before the sole Post click.
                        state.finish(args.account, args.request_id, "submitting")
                        submitted = True
                        await adapter.submit()
                        url = await adapter.confirm(account["handle"], caption, args.privacy)
                        state.finish(args.account, args.request_id, "published", post_url=url)
        except Exception as exc:
            try:
                row = state.get(args.account, args.request_id)
            except Exception:
                row = None
            if row and row["status"] == "published":
                return result(row), 0
            error = exc if isinstance(exc, PublishError) else PublishError(
                "SUBMISSION_UNCERTAIN" if submitted else "TECHNICAL_FAILURE",
                "The browser operation failed or timed out; inspect local diagnostics.", 4 if submitted else 3)
            url = adapter.post_url if adapter else None
            status = "uncertain" if submitted else "failed"
            try:
                state.finish(args.account, args.request_id, status, error.json(), url)
                output = result(state.get(args.account, args.request_id))
            except Exception:
                output = {"platform": "tiktok", "account": args.account, "request_id": args.request_id,
                          "status": status, "post_url": url,
                          "error": {"code": "JOURNAL_FAILURE", "message": "Could not persist the result; inspect the journal and this request manually."}}
            return output, 4 if submitted else error.exit_code
        return result(state.get(args.account, args.request_id)), 0


async def wait_for_enter(stream=None):
    """Read stdin on POSIX readiness, so cancellation leaves no blocked thread."""
    stream = sys.stdin if stream is None else stream
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    seen_data = False
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError) as exc:
        raise PublishError("INTERACTIVE_LOGIN_REQUIRED", "Visible login needs readable terminal stdin.") from exc

    def read_ready():
        nonlocal seen_data
        if ready.done():
            return
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            ready.set_exception(exc)
            return
        seen_data = seen_data or bool(data)
        if not data or b"\n" in data:
            ready.set_result(seen_data)

    try:
        loop.add_reader(fd, read_ready)
    except (OSError, NotImplementedError) as exc:
        raise PublishError("INTERACTIVE_LOGIN_REQUIRED", "Visible login needs stdin supported by the POSIX event loop.") from exc
    try:
        return await ready
    finally:
        loop.remove_reader(fd)


async def login(state: State, args, session_factory=None):
    session_factory = browser_session if session_factory is None else session_factory
    imported = getattr(args, "import_firefox", None)
    expected = getattr(args, "handle", None)
    if imported and not expected:
        raise PublishError("EXPECTED_HANDLE_REQUIRED", "Firefox import requires --handle with the expected TikTok username.")
    if expected:
        expected = expected.removeprefix("@").lower()
        if not re.fullmatch(r"[a-z0-9._]+", expected):
            raise PublishError("INVALID_HANDLE", "Expected TikTok username must contain ASCII letters, digits, . or _.")
    with state.lock(args.account):
        profile = state.profile(args.account)
        cookies = None
        if imported:
            if any(profile.iterdir()):
                raise PublishError("FIREFOX_IMPORT_CONFLICT", "Import requires an unused Camoufox profile; renew an existing session through visible Camoufox login and keep its journal.")
            cookies = firefox_cookies(imported)
        try:
            async with asyncio.timeout(args.timeout_seconds):
                async with session_factory(profile, False) as context:
                    try:
                        if cookies is not None:
                            try:
                                await context.add_cookies(cookies)
                            except Exception:
                                raise PublishError("FIREFOX_COOKIE_IMPORT_FAILED", "Camoufox could not accept the Firefox cookie snapshot; no account was bound.") from None
                        else:
                            page = await context.new_page()
                            await page.goto("https://www.tiktok.com/login?lang=en", wait_until="domcontentloaded")
                            print("Sign in in the visible Camoufox window, finish 2FA, then press Enter here.", file=sys.stderr)
                            if not await wait_for_enter():
                                raise PublishError("INTERACTIVE_LOGIN_REQUIRED", "Visible login requires an interactive terminal.")
                            await page.close()
                        handle = await current_handle(context)
                        if expected and handle != expected:
                            raise PublishError("ACCOUNT_MISMATCH", "Authenticated TikTok username differs from --handle; no account was bound.")
                    except BaseException:
                        if cookies is not None:
                            try:
                                await context.clear_cookies()
                            except Exception:
                                pass
                        raise
            state.bind(args.account, handle)
        except BaseException:
            if cookies is not None:
                shutil.rmtree(profile)
            raise
        return {"platform": "tiktok", "account": args.account, "status": "authenticated", "error": None}, 0


async def accept_ui(state: State, args, session_factory=browser_session, adapter_factory=TikTokStudio):
    """Exercise the real pre-submit checks; never create a request or call submit."""
    with state.lock(args.account):
        account = state.account(args.account)
        directory = state.root / "accounts" / args.account
        temporary = Path(tempfile.mkdtemp(prefix="preflight-", dir=directory))
        try:
            arguments = normalized(args)
            video = temporary / ("video" + Path(arguments["video"]).suffix.lower())
            copy_file(arguments["video"], video)
            probe_video(video)
            cover = None
            if arguments["cover"]:
                cover = temporary / ("cover" + Path(arguments["cover"]).suffix.lower())
                copy_file(arguments["cover"], cover)
                probe_cover(cover)
            caption = read_caption(arguments["caption_file"], arguments["metadata_file"])
            async with asyncio.timeout(args.timeout_seconds):
                async with session_factory(state.profile(args.account), False) as context:
                    adapter = adapter_factory(context, directory / "diagnostics")
                    async with capture_diagnostics(adapter):
                        await adapter.open_editor(account["handle"])
                        await adapter.prepare(video, caption, args.privacy, args.ai_generated, cover)
                        await adapter.verify(account["handle"], caption, args.privacy, args.ai_generated, cover)
                        screenshot = await adapter.screenshot("accepted-editor.png")
            return {"platform": "tiktok", "account": args.account, "status": "accepted",
                    "privacy": args.privacy, "submitted": False, "screenshot": screenshot, "error": None}, 0
        finally:
            shutil.rmtree(temporary)


async def reconcile(state: State, args, session_factory=browser_session, adapter_factory=TikTokStudio):
    with state.lock(args.account):
        supplied_url = getattr(args, "post_url", None)
        confirmed_video = getattr(args, "confirm_request_video", False)
        if bool(supplied_url) != confirmed_video:
            raise PublishError("MANUAL_CONFIRMATION_REQUIRED", "--post-url and --confirm-request-video must be supplied together after comparing this post's video with the request.")
        row = state.get(args.account, args.request_id)
        if row is None:
            raise PublishError("REQUEST_NOT_FOUND", "No journal entry for this account and request ID.")
        row = state.recover(row)
        account = state.account(args.account)
        if supplied_url:
            match = POST_RE.fullmatch(supplied_url)
            if not match or match.group(1).lower() != account["handle"] or urlparse(supplied_url).path not in {
                f"/@{match.group(1)}/video/{match.group(2)}", f"/@{match.group(1)}/video/{match.group(2)}/"
            }:
                raise PublishError("INVALID_POST_URL", "Manual post URL must identify an exact TikTok video in this bound account.")
            supplied_url = f"https://www.tiktok.com/@{account['handle']}/video/{match.group(2)}"
            if row["post_url"] and row["post_url"] != supplied_url:
                raise PublishError("REQUEST_POST_CONFLICT", "This request already identifies another post URL; its binding cannot be replaced.")
        if row["status"] == "published":
            return result(row), 0
        post_url = row["post_url"] or supplied_url
        if row["status"] != "uncertain" or not post_url:
            raise PublishError("RECONCILE_UNAVAILABLE", "Reconciliation needs an uncertain request with a saved URL, or --post-url plus --confirm-request-video after manual video comparison.")
        details = json.loads(row["details"])
        try:
            async with asyncio.timeout(args.timeout_seconds):
                async with session_factory(state.profile(args.account), args.headless) as context:
                    await verify_account(context, account["handle"])
                    adapter = adapter_factory(context, state.request_dir(args.account, args.request_id) / "diagnostics")
                    await adapter.verify_post(post_url, account["handle"], details["caption"], details["privacy"])
                    state.finish(args.account, args.request_id, "published", post_url=post_url)
        except Exception as exc:
            saved = state.get(args.account, args.request_id)
            if saved["status"] == "published":
                return result(saved), 0
            error = exc if isinstance(exc, PublishError) else PublishError("RECONCILE_UNCONFIRMED", "Could not confirm the saved post; no upload or Post was attempted.", 4)
            state.finish(args.account, args.request_id, "uncertain", error.json(), row["post_url"])
            return result(state.get(args.account, args.request_id)), 4
        return result(state.get(args.account, args.request_id)), 0
