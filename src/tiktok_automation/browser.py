"""Conservative TikTok Studio adapter. No retry is allowed for the Post click."""

import asyncio
from contextlib import asynccontextmanager
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .errors import PublishError
from .state import private_dir

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=webapp&lang=en&tab=video"
CONTENT_URL = "https://www.tiktok.com/tiktokstudio/content?lang=en"
PRIVACY = {"everyone": "Everyone", "friends": "Friends", "only-you": "Only me"}
PRIVACY_RE = re.compile(r"^(Everyone|Friends|Only me|Only you)$")
POST_RE = re.compile(r"^https://(?:www\.)?tiktok\.com/@([A-Za-z0-9._]+)/video/(\d+)(?:[/?].*)?$")
SUCCESS_MESSAGE = re.compile(r"^(Post published|Video published|Successfully posted|Your video has been posted)$", re.I)
SUCCESS_LINK = re.compile(r"^View (post|video)$", re.I)
SUCCESS_TIMEOUT_MS = 60000


def rendered_caption_matches(actual, expected):
    """TikTok renders spaces/newlines as one space; preserve every other character."""
    if not isinstance(actual, str):
        return False
    return re.sub(r"[ \t\r\n]+", " ", actual).strip(" \t\r\n") == re.sub(r"[ \t\r\n]+", " ", expected).strip(" \t\r\n")


def verify_browser_build():
    from camoufox.browser_pin import load_pin
    from camoufox.multiversion import get_active_path

    pin = load_pin()
    executable = os.environ.get("CAMOUFOX_EXECUTABLE_PATH")
    if executable:
        metadata = next((p / "version.json" for p in Path(executable).resolve().parents if (p / "version.json").is_file()), None)
    else:
        active = get_active_path()
        metadata = active / "version.json" if active else None
    if not metadata or not metadata.is_file():
        raise PublishError("BROWSER_MISSING", "Install the paired Camoufox browser with python -m camoufox fetch.", 3)
    actual = json.loads(metadata.read_text(encoding="utf-8"))
    if not pin or (actual.get("version"), actual.get("build")) != (pin.version, pin.build):
        raise PublishError("BROWSER_VERSION_MISMATCH", "Browser build differs from the pinned Camoufox package's paired release.", 3)


@asynccontextmanager
async def browser_session(profile: Path, headless=False):
    from camoufox.fingerprints import generate_fingerprint
    from camoufox.async_api import AsyncCamoufox

    verify_browser_build()
    fingerprint_path = profile.parent / "fingerprint.json"
    if fingerprint_path.exists():
        fingerprint = json.loads(fingerprint_path.read_text(encoding="utf-8"))
    else:
        fingerprint = generate_fingerprint(os="macos" if sys.platform == "darwin" else "linux")
        with fingerprint_path.open("w", encoding="utf-8") as stream:
            fingerprint_path.chmod(0o600)
            json.dump(fingerprint, stream)
    mode = "virtual" if headless and sys.platform.startswith("linux") else headless
    # Third-party initialization/progress must not pollute machine-readable stdout.
    try:
        async with AsyncCamoufox(persistent_context=True, user_data_dir=str(profile), fingerprint=fingerprint,
                                headless=mode, humanize=1.0, locale="en-US", block_images=False,
                                main_world_eval=True,
                                i_know_what_im_doing=True) as context:
            yield context
    finally:
        for parent, dirs, files in os.walk(profile):
            Path(parent).chmod(0o700)
            for name in files:
                path = Path(parent) / name
                if not path.is_symlink():
                    path.chmod(0o600)


async def one(locator, description):
    count = await locator.count()
    if count != 1:
        raise PublishError("UI_CHANGED", f"Cannot uniquely locate {description}; found {count} controls.", 3)
    return locator


async def current_handle(context) -> str:
    """Use the authenticated navigation's own Profile link, never display name."""
    page = await context.new_page()
    try:
        await page.goto("https://www.tiktok.com/?lang=en", wait_until="domcontentloaded")
        profile = page.get_by_role("link", name="Profile", exact=True).or_(
            page.locator('a[data-e2e="nav-profile"]')
        ).filter(visible=True)
        await profile.first.wait_for(state="visible", timeout=30000)
        href = await (await one(profile, "authenticated Profile navigation")).get_attribute("href")
        if not href:
            raise PublishError("LOGIN_REQUIRED", "Cannot verify current account; run visible login again.")
        url = urlparse(href)
        if url.netloc and url.netloc not in {"www.tiktok.com", "tiktok.com"}:
            raise PublishError("LOGIN_REQUIRED", "Profile navigation does not identify a TikTok account.")
        match = re.fullmatch(r"/@([A-Za-z0-9._]+)/?", url.path)
        if not match:
            raise PublishError("LOGIN_REQUIRED", "Cannot verify current account; run visible login again.")
        return match.group(1).lower()
    except PublishError:
        raise
    except Exception as exc:
        raise PublishError("LOGIN_REQUIRED", "Authenticated Profile navigation is unavailable; run visible login again.") from exc
    finally:
        await page.close()


async def verify_account(context, expected):
    if await current_handle(context) != expected:
        raise PublishError("ACCOUNT_MISMATCH", "Current TikTok account differs from the bound profile; run visible login.")


class TikTokStudio:
    def __init__(self, context, diagnostics: Path):
        self.context = context
        self.diagnostics = private_dir(diagnostics)
        self.page = None
        self.post_url = None
        self.cover_ready = False
        self.submission_started = False

    async def screenshot(self, name="last-failure.png"):
        if self.page and not self.page.is_closed():
            path = self.diagnostics / name
            try:
                await self.page.screenshot(path=str(path), full_page=False, timeout=5000)
                path.chmod(0o600)
                return str(path)
            except Exception:
                pass
        return None

    async def open_editor(self, handle):
        await verify_account(self.context, handle)
        self.page = await self.context.new_page()
        await self.page.goto(UPLOAD_URL, wait_until="domcontentloaded")
        await self.page.locator('input[type="file"][accept*="video"]').wait_for(state="attached", timeout=30000)

    async def posts(self):
        page = await self.context.new_page()
        await page.goto(CONTENT_URL, wait_until="domcontentloaded")
        return page

    async def caption_editor(self):
        editor = self.page.get_by_role("combobox", name="Description", exact=True)
        if await editor.count() == 0:
            editor = self.page.locator('[contenteditable="true"][data-text="true"], [contenteditable="true"][aria-label="Description"], [contenteditable="true"].public-DraftEditor-content')
        return await one(editor, "Description editor")

    def edit_cover_control(self):
        return self.page.get_by_role("button", name="Edit cover", exact=True).or_(
            self.page.locator('[data-e2e="cover_container"]').get_by_text("Edit cover", exact=True)
        )

    async def dismiss_editor_onboarding(self):
        checks = self.page.get_by_role("dialog").filter(has=self.page.get_by_text("Turn on automatic content checks?", exact=True)).filter(visible=True)
        if await checks.count():
            dialog = await one(checks, "automatic checks onboarding")
            await (await one(dialog.get_by_role("button", name="Cancel", exact=True), "automatic checks Cancel")).click()
            await dialog.wait_for(state="hidden")
        for _ in range(4):
            tutorial = self.page.locator('.tutorial-tooltip').filter(visible=True)
            if not await tutorial.count():
                break
            await (await one(tutorial.get_by_role("button", name="Got it", exact=True), "editor tutorial Got it")).click()
            await self.page.wait_for_timeout(150)

    async def caption_text(self):
        editor = await self.caption_editor()
        tag = await editor.evaluate("e => e.tagName.toLowerCase()")
        blocks = editor.locator('[data-block="true"]')
        if "public-DraftEditor-content" in (await editor.get_attribute("class") or "") and await blocks.count():
            return await blocks.evaluate_all('els => els.map(e => e.textContent || "").join("\\n")')
        return await editor.input_value() if tag in {"textarea", "input"} else await editor.inner_text()

    async def fill_caption(self, caption):
        editor = await self.caption_editor()
        if "public-DraftEditor-content" in (await editor.get_attribute("class") or ""):
            # Draft must receive its paste event to update React's saved state.
            # A DOM-only fill can revert to the filename on the next render.
            await editor.click()
            await editor.press("ControlOrMeta+A")
            await editor.press("Backspace")
            await self.page.wait_for_timeout(100)
            await self.page.evaluate('''mw:(text) => {
                const element = document.querySelector('[contenteditable="true"].public-DraftEditor-content');
                const event = new Event("paste", {bubbles: true, cancelable: true});
                Object.defineProperty(event, "clipboardData", {value: {
                    types: ["text/plain"], files: [], items: [],
                    getData: type => type === "text/plain" || type === "Text" ? text : ""
                }});
                element.dispatchEvent(event);
            }''', caption)
            await self.page.wait_for_timeout(150)
        else:
            await editor.fill(caption)

    async def privacy_control(self, page=None):
        page = page or self.page
        locator = page.get_by_role("combobox", name=PRIVACY_RE).or_(
            page.get_by_role("combobox").filter(has_text=PRIVACY_RE)
        ).filter(visible=True)
        return await one(locator, "audience")

    async def privacy_label(self, control):
        text = (await control.inner_text()).strip()
        if text in {"Everyone", "Friends", "Only me", "Only you"}:
            return text
        name = await control.get_attribute("aria-label")
        if name in {"Everyone", "Friends", "Only me", "Only you"}:
            return name
        raise PublishError("PRIVACY_UNVERIFIED", "Cannot read the selected audience.")

    async def select_privacy(self, control, privacy, page=None):
        page = page or self.page
        wanted = PRIVACY[privacy]
        labels = {wanted, "Only you"} if privacy == "only-you" else {wanted}
        option_name = re.compile(r"^Only (me|you)$") if privacy == "only-you" else wanted
        if await self.privacy_label(control) not in labels:
            await control.click()
            option = page.get_by_role("option", name=option_name, exact=True)
            if await option.count() == 0:
                option = page.get_by_text(option_name, exact=True)
            if await option.count() == 0:
                raise PublishError("PRIVACY_UNAVAILABLE", "Requested audience is not available for this account.")
            await (await one(option, f"{wanted} audience option")).click()
        actual = await self.privacy_label(control)
        if actual not in labels:
            raise PublishError("PRIVACY_UNAVAILABLE", "Requested audience is unavailable or did not remain selected.")

    async def ai_switch(self):
        switch = self.page.get_by_role("switch", name="AI-generated content", exact=True)
        if await switch.count() == 0:
            label = await one(self.page.get_by_text("AI-generated content", exact=True), "AI-generated content label")
            switch = label.locator("xpath=..").get_by_role("switch")
            if await switch.count() == 0:
                switch = label.locator("xpath=..").get_by_role("checkbox")
            if await switch.count() == 0:
                switch = self.page.locator('[data-e2e="aigc_container"] [aria-checked]')
        return await one(switch, "AI-generated content toggle")

    async def ai_enabled(self):
        switch = await self.ai_switch()
        checked = await switch.get_attribute("aria-checked")
        if checked in {"true", "false"}:
            return checked == "true"
        return await switch.is_checked()

    async def prepare(self, video, caption, privacy, ai_generated, cover):
        upload = await one(self.page.locator('input[type="file"][accept*="video"]'), "video upload input")
        await upload.set_input_files(str(video))
        await self.edit_cover_control().first.wait_for(state="visible", timeout=120000)
        await self.dismiss_editor_onboarding()
        await self.fill_caption(caption)
        await self.select_privacy(await self.privacy_control(), privacy)
        more = self.page.get_by_text("Show more", exact=True).filter(visible=True)
        if await more.count():
            await (await one(more, "advanced settings")).click()
        if await self.ai_enabled() != ai_generated:
            await (await self.ai_switch()).click()
            if ai_generated:
                confirm = self.page.get_by_role("button", name="Turn on", exact=True)
                try:
                    await confirm.wait_for(state="visible", timeout=5000)
                except Exception:
                    if not await self.ai_enabled():
                        raise PublishError("AI_LABEL_MISMATCH", "AI-generated content did not turn on.") from None
                else:
                    await confirm.click()
        if cover:
            await (await one(self.edit_cover_control(), "Edit cover")).click()
            dialog = await one(self.page.get_by_role("dialog"), "cover dialog")
            chooser = dialog.get_by_role("button", name="Upload cover image", exact=True)
            async with self.page.expect_file_chooser() as pending:
                await chooser.click()
            await (await pending.value).set_files(str(cover))
            await dialog.get_by_role("button", name="Uploaded cover image", exact=True).wait_for(timeout=30000)
            await dialog.get_by_role("button", name="Save", exact=True).click()
            await dialog.wait_for(state="hidden")
            self.cover_ready = True
        await self.page.get_by_role("button", name="Post", exact=True).wait_for(state="visible", timeout=120000)

    async def verify(self, handle, caption, privacy, ai_generated, cover):
        await verify_account(self.context, handle)
        if await self.caption_text() != caption:
            raise PublishError("CAPTION_MISMATCH", "Description did not preserve the exact requested caption.")
        actual = await self.privacy_label(await self.privacy_control())
        if actual != PRIVACY[privacy] and not (privacy == "only-you" and actual == "Only you"):
            raise PublishError("PRIVACY_MISMATCH", "Selected audience differs from the request.")
        if await self.ai_enabled() != ai_generated:
            raise PublishError("AI_LABEL_MISMATCH", "AI-generated content setting differs from the request.")
        if ai_generated:
            await (await one(self.page.get_by_text("Creator labeled as AI-generated", exact=True), "AI label preview")).wait_for(state="visible")
        elif await self.page.get_by_text("Creator labeled as AI-generated", exact=True).filter(visible=True).count():
            raise PublishError("AI_LABEL_MISMATCH", "AI label preview is still enabled for a request without the label.")
        now = await one(self.page.get_by_role("radio", name="Now", exact=True), "immediate publication")
        if not await now.is_checked():
            raise PublishError("SCHEDULE_UNSUPPORTED", "Studio is not set to publish Now.")
        if cover and not self.cover_ready:
            raise PublishError("COVER_UNVERIFIED", "Requested cover was not accepted and saved.")
        if cover:
            # Reopen the saved cover to verify persisted UI state before Post.
            await (await one(self.edit_cover_control(), "Edit cover")).click()
            dialog = await one(self.page.get_by_role("dialog"), "saved cover dialog")
            await dialog.get_by_role("button", name="Uploaded cover image", exact=True).wait_for(state="visible", timeout=10000)
            await dialog.get_by_role("button", name="Save", exact=True).click()
            await dialog.wait_for(state="hidden")
        post_control = self.page.get_by_role("button", name="Post", exact=True)
        await post_control.first.wait_for(state="visible", timeout=30000)
        post = await one(post_control, "Post button")
        if not await post.is_enabled():
            raise PublishError("UPLOAD_NOT_READY", "Video processing or a required setting prevents Post.", 3)

    async def submit(self):
        # Exactly one click; no exception handler can re-enter this method.
        if self.submission_started or await self.success_dialogs().count():
            raise PublishError("SUBMISSION_UNSAFE", "Submission was already started or a success dialog predates this Post.", 4)
        self.submission_started = True
        await self.page.get_by_role("button", name="Post", exact=True).click(timeout=30000)

    def success_dialogs(self):
        return self.page.get_by_role("dialog").filter(has=self.page.get_by_text(SUCCESS_MESSAGE)).filter(visible=True)

    async def confirm(self, handle, caption, privacy):
        if not self.submission_started:
            raise PublishError("POST_UNCONFIRMED", "No Post click was started in this editor.", 4)
        # Only this editor's Post success dialog can identify its submitted item.
        # A changed Posts list or an identical caption is never causal evidence.
        dialog = self.success_dialogs()
        try:
            # Studio can redirect before mounting the personal success dialog.
            # A redirect alone neither identifies the post nor ends this wait.
            await dialog.first.wait_for(state="visible", timeout=SUCCESS_TIMEOUT_MS)
        except Exception:
            raise PublishError("CAUSAL_POST_ID_MISSING", "This Post did not expose a personal success link; inspect the account manually without reposting.", 4) from None
        if await dialog.count() != 1:
            raise PublishError("CAUSAL_POST_ID_MISSING", "This Post did not expose a personal success link; inspect the account manually without reposting.", 4)
        link = dialog.get_by_role("link", name=SUCCESS_LINK)
        await link.first.wait_for(state="visible", timeout=10000)
        href = await (await one(link, "this Post's success link")).get_attribute("href")
        url = urljoin(self.page.url, href or "")
        match = POST_RE.fullmatch(url)
        if not match or match.group(1).lower() != handle:
            raise PublishError("POST_UNCONFIRMED", "This Post's success link does not identify the selected account.", 4)
        url = f"https://www.tiktok.com/@{handle}/video/{match.group(2)}"
        # Keep the URL only after this exact ID's author and caption also match.
        await self.verify_item(url, handle, caption)
        self.post_url = url
        await self.verify_post(url, handle, caption, privacy)
        return self.post_url

    async def verify_item(self, url, handle, caption):
        match = POST_RE.fullmatch(url)
        response = await self.context.request.get(url, timeout=30000)
        item = video_item(await response.text()) if response.ok else None
        if not match or not item or str(item.get("id")) != match.group(2) or item.get("author", {}).get("uniqueId", "").lower() != handle or not rendered_caption_matches(item.get("desc"), caption):
            raise PublishError("POST_UNCONFIRMED", "Submitted post identity or caption could not be verified.", 4)

    async def verify_post(self, url, handle, caption, privacy):
        match = POST_RE.fullmatch(url)
        if not match or match.group(1).lower() != handle:
            raise PublishError("POST_UNCONFIRMED", "Saved post URL does not identify this account.", 4)
        page = await self.posts()
        try:
            path = f"/@{match.group(1)}/video/{match.group(2)}"
            hrefs = [path, "https://www.tiktok.com" + path, "https://tiktok.com" + path]
            selector = ", ".join(f'a[href={json.dumps(href + suffix)}], a[href^={json.dumps(href + suffix + "?")}]'
                                 for href in hrefs for suffix in ("", "/"))
            links = page.locator(selector)
            await links.first.wait_for(state="visible", timeout=30000)
            link = await one(links, "known submitted post link")
            row = link.locator("xpath=ancestor::*[.//button[normalize-space()='Everyone' or normalize-space()='Friends' or normalize-space()='Only me' or normalize-space()='Only you']][1]")
            row = await one(row, "submitted post row")
            row_urls = await row.locator('a[href*="/video/"]').evaluate_all("els => els.map(e => e.href)")
            if any((other := POST_RE.fullmatch(href)) and other.group(2) != match.group(2) for href in row_urls):
                raise PublishError("POST_AUDIENCE_UNCONFIRMED", "The known post has no uniquely scoped audience control.", 4)
            audience = await one(row.get_by_role("button", name=PRIVACY_RE), "post audience")
            actual = await self.privacy_label(audience)
            if actual != PRIVACY[privacy] and not (privacy == "only-you" and actual == "Only you"):
                raise PublishError("POST_PRIVACY_MISMATCH", "Submitted post audience differs from the requested audience.", 4)
            if await row.get_by_text("Content under review", exact=True).count():
                raise PublishError("CONTENT_UNDER_REVIEW", "TikTok is reviewing this post; inspect the saved URL and reconcile later.", 4)
            await self.verify_item(url, handle, caption)
            if privacy == "everyone":
                guest = await asyncio.to_thread(guest_item, url)
                if not guest:
                    guest = await guest_browser_item(self.context, url)
                if not guest or str(guest.get("id")) != match.group(2) or guest.get("author", {}).get("uniqueId", "").lower() != handle or not rendered_caption_matches(guest.get("desc"), caption) or guest.get("privateItem") is not False or not guest.get("video", {}).get("playAddr"):
                    raise PublishError("PUBLIC_VISIBILITY_UNCONFIRMED", "Guest access did not confirm this exact public playable post.", 4)
        finally:
            await page.close()


class HydrationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__UNIVERSAL_DATA_FOR_REHYDRATION__":
            self.inside = True

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False


def video_item(html):
    parser = HydrationParser()
    parser.feed(html)
    try:
        data = json.loads("".join(parser.parts))
        return data["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"]["itemStruct"]
    except (ValueError, KeyError, TypeError):
        return None


def guest_item(url):
    try:
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}), timeout=30) as response:
            return video_item(response.read(8 * 1024**2).decode("utf-8"))
    except Exception:
        return None


async def guest_browser_item(authenticated_context, url):
    """Read public hydration in a fresh cookie-free Camoufox context."""
    context = None
    try:
        browser = authenticated_context.browser
        if browser is None:
            return None
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        script = page.locator('script#__UNIVERSAL_DATA_FOR_REHYDRATION__')
        await script.wait_for(state="attached", timeout=30000)
        return video_item(await page.content())
    except Exception:
        return None
    finally:
        if context is not None:
            await context.close()
