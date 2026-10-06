"""Controlled local DOM exercises the adapter, without a TikTok login or post."""
import json
from pathlib import Path

import pytest
from tiktok_automation.browser import CONTENT_URL, UPLOAD_URL, TikTokStudio, browser_session, current_handle, video_item
from tiktok_automation.state import private_dir
from tiktok_automation.errors import PublishError
from tiktok_automation.browser import rendered_caption_matches


HTML = """<label>Description<textarea role=combobox aria-label=Description></textarea></label>
<button role=combobox aria-label=Everyone onclick="document.getElementById('friends').hidden=false">Everyone</button>
<button id=friends role=option hidden onclick="document.querySelector('[aria-label=Everyone]').textContent='Friends';this.hidden=true">Friends</button>
<button role=switch aria-label="AI-generated content" aria-checked=false onclick="this.setAttribute('aria-checked',this.getAttribute('aria-checked')==='false'?'true':'false')"></button>
<label><input type=radio checked>Now</label>
<button onclick="document.body.dataset.clicks=Number(document.body.dataset.clicks||0)+1">Post</button>"""


@pytest.fixture
async def page(tmp_path):
    async with browser_session(private_dir(tmp_path / "profile"), headless=True) as context:
        page = await context.new_page()
        await page.set_content(HTML)
        yield page


async def test_caption_privacy_ai_and_one_click(page, tmp_path):
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    adapter.page = page
    await (await adapter.caption_editor()).fill("  Exact caption\n#tag")
    assert await adapter.caption_text() == "  Exact caption\n#tag"
    await adapter.select_privacy(await adapter.privacy_control(), "friends")
    assert await adapter.privacy_label(await adapter.privacy_control()) == "Friends"
    assert await adapter.ai_enabled() is False
    await (await adapter.ai_switch()).click()
    assert await adapter.ai_enabled() is True
    await adapter.submit()
    assert await page.locator("body").get_attribute("data-clicks") == "1"


async def test_unreadable_privacy_fails(page, tmp_path):
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    await page.set_content('<button role=combobox aria-label=Audience>Followers</button>')
    with pytest.raises(PublishError) as exc:
        await adapter.privacy_label(page.get_by_role("combobox"))
    assert exc.value.code == "PRIVACY_UNVERIFIED"


async def test_only_you_editor_label_maps_to_only_me_request(page, tmp_path):
    await page.set_content('''<button role=combobox aria-label=Everyone onclick="document.getElementById('only').hidden=false">Everyone</button>
      <button id=only role=option hidden onclick="document.querySelector('[role=combobox]').textContent='Only you';this.hidden=true">Only you</button>''')
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    adapter.page = page
    await adapter.select_privacy(await adapter.privacy_control(), "only-you")
    assert await adapter.privacy_label(await adapter.privacy_control()) == "Only you"


async def test_observed_editor_controls_and_onboarding(page, tmp_path):
    await page.set_content('''<div role="dialog"><span>Turn on automatic content checks?</span>
        <button onclick="this.closest('[role=dialog]').remove()">Cancel</button></div>
      <div class="tutorial-tooltip"><span>New editing features added</span>
        <button onclick="this.parentElement.remove()">Got it</button></div>
      <div data-e2e="cover_container"><div>Edit cover</div></div>
      <div contenteditable="true" role="combobox" class="public-DraftEditor-content" style="white-space:pre-wrap" onpaste="event.preventDefault();this.textContent=event.clipboardData.getData('text/plain')"></div>
      <span id="audience-label">Who can see this post</span>
      <button role="combobox" aria-labelledby="audience-label">Everyone</button>
      <div data-e2e="aigc_container"><span>AI-generated content</span>
        <div aria-checked="false" onclick="this.setAttribute('aria-checked','true')"><input role="switch" type="checkbox" aria-hidden="true"></div></div>''')
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    adapter.page = page
    await adapter.dismiss_editor_onboarding()
    assert await page.get_by_role("dialog").count() == 0
    assert await page.locator(".tutorial-tooltip").count() == 0
    assert await adapter.edit_cover_control().count() == 1
    await adapter.fill_caption("Exact\nCaption")
    assert await adapter.caption_text() == "Exact\nCaption"
    assert await adapter.privacy_label(await adapter.privacy_control()) == "Everyone"
    assert await adapter.ai_enabled() is False
    await (await adapter.ai_switch()).click()
    assert await adapter.ai_enabled() is True


async def test_draft_caption_reads_exact_plain_text_blocks(page, tmp_path):
    await page.set_content('<div contenteditable="true" role="combobox" class="public-DraftEditor-content"><div data-block="true">Exact</div><div data-block="true"><br></div><div data-block="true">Caption</div></div>')
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    adapter.page = page
    assert await adapter.caption_text() == "Exact\n\nCaption"


async def test_verification_rejects_wrong_identity_and_settings(page, tmp_path):
    async def authenticated_navigation(route):
        await route.fulfill(body='<a href="/@ExactUser">Profile</a>', content_type="text/html")
    await page.context.route("https://www.tiktok.com/**", authenticated_navigation)
    assert await current_handle(page.context) == "exactuser"
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    adapter.page = page
    await (await adapter.caption_editor()).fill("Exact caption")
    cases = [("other", "Exact caption", "everyone", False, "ACCOUNT_MISMATCH"),
             ("exactuser", "Changed caption", "everyone", False, "CAPTION_MISMATCH"),
             ("exactuser", "Exact caption", "friends", False, "PRIVACY_MISMATCH"),
             ("exactuser", "Exact caption", "everyone", True, "AI_LABEL_MISMATCH")]
    for handle, caption, privacy, ai, expected in cases:
        with pytest.raises(PublishError) as exc:
            await adapter.verify(handle, caption, privacy, ai, None)
        assert exc.value.code == expected
    await adapter.verify("exactuser", "Exact caption", "everyone", False, None)
    assert await page.locator("body").get_attribute("data-clicks") is None


async def test_identity_uses_live_profile_navigation_with_listitem_child(page):
    async def navigation(route):
        await route.fulfill(body='<a data-e2e="nav-profile" href="/@ExactUser?lang=en"><button role="listitem"><div>Profile</div></button></a>', content_type="text/html")
    await page.context.route("https://www.tiktok.com/**", navigation)
    assert await current_handle(page.context) == "exactuser"


def test_structured_post_identity():
    item = {"author": {"uniqueId": "test"}, "desc": "Exact", "privateItem": False, "video": {"playAddr": "https://example.test/video"}}
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}}}}
    html = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">' + json.dumps(data) + '</script>'
    assert video_item(html) == item
    assert video_item('<script id="other">{}</script>') is None


def test_rendered_caption_allows_only_space_and_newline_normalization():
    expected = "Quiz 🏝️\n\n#WordOrigins #Caribbean"
    assert rendered_caption_matches("Quiz 🏝️ #WordOrigins #Caribbean", expected)
    assert not rendered_caption_matches("Quiz 🏝️ #WordOrigins #Changed", expected)
    assert not rendered_caption_matches("Quiz🏝️ #WordOrigins #Caribbean", expected)
    assert not rendered_caption_matches("Quiz 🏝️ #Caribbean #WordOrigins", expected)
    assert not rendered_caption_matches(None, expected)


def item_document(video_id="123", caption="Repeated caption"):
    item = {"id": video_id, "author": {"uniqueId": "test"}, "desc": caption}
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}}}}
    return '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">' + json.dumps(data) + '</script>'


async def test_competing_identical_caption_is_not_claimed_or_modified(page, tmp_path, monkeypatch):
    monkeypatch.setattr("tiktok_automation.browser.SUCCESS_TIMEOUT_MS", 500)
    competing_url = "https://www.tiktok.com/@test/video/456"
    async def route_editor(route):
        if "tiktokstudio/content" in route.request.url:
            body = '<h1>Posts</h1><div><a href="' + competing_url + '">Repeated caption</a><button>Only me</button></div>'
            body += item_document("456")
        else:
            body = '<button onclick="location.href=' + "'" + CONTENT_URL + "'" + '">Post</button>'
        await route.fulfill(body=body, content_type="text/html")
    await page.context.route("https://www.tiktok.com/**", route_editor)
    await page.goto(UPLOAD_URL)
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    adapter.page = page
    async def forbidden(*args, **kwargs):
        raise AssertionError("A competing general-list post must not be inspected or modified")
    monkeypatch.setattr(page.context.request, "get", forbidden)
    monkeypatch.setattr(adapter, "select_privacy", forbidden)
    await adapter.submit()
    with pytest.raises(PublishError) as exc:
        await adapter.confirm("test", "Repeated caption", "everyone")
    assert exc.value.code == "CAUSAL_POST_ID_MISSING"
    assert exc.value.exit_code == 4 and adapter.post_url is None
    assert await page.get_by_role("button", name="Only me", exact=True).count() == 1


async def test_known_post_waits_for_exact_id_after_async_loading(page, tmp_path, monkeypatch):
    known_url = "https://www.tiktok.com/@test/video/123"
    row = '<div><a href="' + known_url + '">Repeated caption</a><button>Only me</button></div>'
    async def route_posts(route):
        body = '<h1>Posts</h1><div id="rows"><div><a href="https://www.tiktok.com/@test/video/1234">Repeated caption</a><button>Everyone</button></div></div>'
        body += '<script>setTimeout(() => document.getElementById("rows").insertAdjacentHTML("beforeend", ' + json.dumps(row) + '), 250)</script>'
        await route.fulfill(body=body, content_type="text/html")
    await page.context.route("https://www.tiktok.com/**", route_posts)
    requests = []
    class Response:
        ok = True
        async def text(self): return item_document()
    async def get(url, **kwargs):
        requests.append(url)
        return Response()
    monkeypatch.setattr(page.context.request, "get", get)
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    await adapter.verify_post(known_url, "test", "Repeated caption", "only-you")
    assert requests == [known_url]


async def test_success_link_confirms_only_this_post(page, tmp_path, monkeypatch):
    known_url = "https://www.tiktok.com/@test/video/123"
    async def route_posts(route):
        body = '<h1>Posts</h1><div><a href="' + known_url + '">Repeated caption</a><button>Only me</button></div>'
        await route.fulfill(body=body, content_type="text/html")
    await page.context.route("https://www.tiktok.com/**", route_posts)
    success = '<div role="dialog"><h2>Post published</h2><a href="' + known_url + '">View post</a></div>'
    await page.set_content('<div id="result"></div><button onclick="document.getElementById(\'result\').innerHTML=' + json.dumps(success).replace('"', '&quot;') + '">Post</button>')
    class Response:
        ok = True
        async def text(self): return item_document()
    async def get(url, **kwargs):
        assert url == known_url
        return Response()
    monkeypatch.setattr(page.context.request, "get", get)
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    adapter.page = page
    await adapter.submit()
    assert await adapter.confirm("test", "Repeated caption", "only-you") == known_url


async def test_redirect_does_not_cancel_delayed_personal_success_link(page, tmp_path, monkeypatch):
    monkeypatch.setattr("tiktok_automation.browser.SUCCESS_TIMEOUT_MS", 1000)
    known_url = "https://www.tiktok.com/@test/video/123"
    success = '<div role="dialog"><h2>Post published</h2><a href="' + known_url + '">View post</a></div>'
    body = '<script>setTimeout(() => document.body.insertAdjacentHTML("beforeend", ' + json.dumps(success) + '), 200)</script>'
    async def route_posts(route):
        await route.fulfill(body=body, content_type="text/html")
    await page.context.route("https://www.tiktok.com/**", route_posts)
    await page.set_content('<button onclick="location.href=' + "'" + CONTENT_URL + "'" + '">Post</button>')
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    adapter.page = page
    checked = []
    async def verify(url, *_):
        assert url == known_url
        checked.append(url)
    monkeypatch.setattr(adapter, "verify_item", verify)
    monkeypatch.setattr(adapter, "verify_post", verify)
    await adapter.submit()
    assert await adapter.confirm("test", "Repeated caption", "everyone") == known_url
    assert checked == [known_url, known_url]


async def test_guest_browser_fallback_has_no_authenticated_cookies(page, monkeypatch):
    from tiktok_automation.browser import guest_browser_item
    url = "https://www.tiktok.com/@test/video/123"
    await page.context.add_cookies([{"name": "sessionid", "value": "private", "url": "https://www.tiktok.com"}])
    original = page.context.browser.new_context
    async def guest(**kwargs):
        context = await original(**kwargs)
        assert await context.cookies() == []
        async def route(request):
            assert "cookie" not in await request.request.all_headers()
            await request.fulfill(body=item_document(), content_type="text/html")
        await context.route("https://www.tiktok.com/**", route)
        return context
    monkeypatch.setattr(page.context.browser, "new_context", guest)
    item = await guest_browser_item(page.context, url)
    assert item["id"] == "123" and item["author"]["uniqueId"] == "test"
    assert (await page.context.cookies(url))[0]["value"] == "private"


async def test_imported_cookies_preserve_host_and_path_scope(page):
    await page.context.add_cookies([
        {"name": "domain", "value": "synthetic", "domain": ".tiktok.com", "path": "/", "secure": True},
        {"name": "host", "value": "synthetic", "domain": "www.tiktok.com", "path": "/tiktokstudio", "secure": True},
    ])
    assert {c["name"] for c in await page.context.cookies("https://www.tiktok.com/tiktokstudio/upload")} == {"domain", "host"}
    assert {c["name"] for c in await page.context.cookies("https://www.tiktok.com/")} == {"domain"}
    received = []
    async def route(request):
        headers = await request.request.all_headers()
        received.append(headers.get("cookie", ""))
        await request.fulfill(body="Cookie scope check", content_type="text/html")
    await page.context.route("https://sub.www.tiktok.com/**", route)
    await page.goto("https://sub.www.tiktok.com/tiktokstudio/upload")
    assert received == ["domain=synthetic"]


async def test_known_post_privacy_mismatch_never_changes_audience(page, tmp_path, monkeypatch):
    known_url = "https://www.tiktok.com/@test/video/123"
    async def route_posts(route):
        body = '<h1>Posts</h1><div><a href="' + known_url + '">Repeated caption</a><button>Only me</button></div>'
        await route.fulfill(body=body, content_type="text/html")
    await page.context.route("https://www.tiktok.com/**", route_posts)
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    async def forbidden(*args, **kwargs):
        raise AssertionError("Post confirmation must not mutate privacy")
    monkeypatch.setattr(adapter, "select_privacy", forbidden)
    with pytest.raises(PublishError) as exc:
        await adapter.verify_post(known_url, "test", "Repeated caption", "everyone")
    assert exc.value.code == "POST_PRIVACY_MISMATCH" and exc.value.exit_code == 4


async def test_known_post_cannot_read_neighbors_audience(page, tmp_path):
    known_url = "https://www.tiktok.com/@test/video/123"
    async def route_posts(route):
        body = '<h1>Posts</h1><section><div><a href="' + known_url + '">Known post</a></div>'
        body += '<div><a href="https://www.tiktok.com/@test/video/456">Other post</a><button>Everyone</button></div></section>'
        await route.fulfill(body=body, content_type="text/html")
    await page.context.route("https://www.tiktok.com/**", route_posts)
    adapter = TikTokStudio(page.context, tmp_path / "diag")
    with pytest.raises(PublishError) as exc:
        await adapter.verify_post(known_url, "test", "Repeated caption", "everyone")
    assert exc.value.code == "POST_AUDIENCE_UNCONFIRMED" and exc.value.exit_code == 4
