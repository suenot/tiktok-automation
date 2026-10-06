from argparse import Namespace
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tiktok_automation.cli import main
from tiktok_automation.errors import PublishError
from tiktok_automation.media import copy_file, read_caption
from tiktok_automation.service import accept_ui, publish, normalized, reconcile
from tiktok_automation.state import State, canonical


def args(tmp_path, **changes):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"first video")
    values = dict(account="test", request_id="one", video=str(video), caption_file=None,
                  metadata_file=None, privacy="everyone", ai_generated=True, cover=None,
                  headless=True, timeout_seconds=30)
    values.update(changes)
    return Namespace(**values)


@asynccontextmanager
async def fake_session(*_):
    yield object()


class Adapter:
    clicks = 0
    fail = None
    video_seen = None
    post_url = None

    def __init__(self, *_):
        pass

    async def open_editor(self, *_):
        pass

    async def prepare(self, video, *_):
        self.video_seen = video

    async def verify(self, *_):
        if self.fail == "verify":
            raise PublishError("ACCOUNT_MISMATCH", "Wrong account")

    async def submit(self):
        type(self).clicks += 1
        if self.fail == "submit":
            raise TimeoutError()

    async def confirm(self, *_):
        if self.fail == "confirm":
            self.post_url = "https://www.tiktok.com/@test/video/123"
            raise PublishError("CONTENT_UNDER_REVIEW", "Reviewing", 4)
        return "https://www.tiktok.com/@test/video/123"

    async def screenshot(self):
        return None


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import tiktok_automation.service as service
    monkeypatch.setattr(service, "probe_video", lambda path: None)
    monkeypatch.setattr(service, "probe_cover", lambda path: None)
    Adapter.clicks, Adapter.fail = 0, None
    state = State(tmp_path / "state")
    state.bind("test", "test")
    return state


@pytest.mark.parametrize("flags,code", [([], 2), (["publish"], 2), (["--help"], 0),
                                       (["publish", "--help"], 0), (["accounts", "list"], 0)])
def test_single_json(flags, code, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TIKTOK_PUBLISH_STATE_DIR", str(tmp_path / "state"))
    assert main(flags) == code
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    assert isinstance(json.loads(out), dict)


def test_early_interrupt_still_outputs_one_json(tmp_path, monkeypatch, capsys):
    import tiktok_automation.cli as cli
    def interrupt(*_):
        raise KeyboardInterrupt()
    monkeypatch.setattr(cli, "State", interrupt)
    assert main(["publish", "--account", "test", "--request-id", "id", "--video", "missing", "--privacy", "everyone"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INTERRUPTED"


def test_caption_preserved_and_full_metadata_required(tmp_path):
    caption = tmp_path / "caption.txt"
    caption.write_bytes("  Exact\r\nCaption #tag\n".encode())
    assert read_caption(str(caption)) == "  Exact\r\nCaption #tag\n"
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"schema_version": "1.0.0", "platforms": {"tiktok": {"caption": "x"}}}))
    with pytest.raises(PublishError, match="invalid"):
        read_caption(metadata_file=str(metadata))


@pytest.mark.parametrize("schema_version", ["1.0.0", "2.0.0"])
def test_valid_metadata_caption_is_not_rebuilt(tmp_path, schema_version):
    from metadata_automation import generate
    envelope = generate({"language": "en", "title": "Example", "summary": "Different description"},
                        schema_version=schema_version)
    envelope["platforms"]["tiktok"] = {"caption": "  Exact\r\nCaption #tag\n", "hashtags": ["#tag"]}
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(envelope))
    assert read_caption(metadata_file=str(path)) == "  Exact\r\nCaption #tag\n"
    envelope["schema_version"] = "3.0.0"
    path.write_text(json.dumps(envelope))
    with pytest.raises(PublishError):
        read_caption(metadata_file=str(path))


def test_snapshot_is_independent_of_source(tmp_path):
    source, copy = tmp_path / "source", tmp_path / "copy"
    source.write_bytes(b"uploaded bytes")
    digest = copy_file(str(source), copy)
    source.write_bytes(b"later source content")
    assert copy.read_bytes() == b"uploaded bytes"
    assert len(digest) == 64
    assert copy.stat().st_mode & 0o777 == 0o600


def test_lock_across_processes(setup):
    code = "from pathlib import Path; from tiktok_automation.state import State; from tiktok_automation.errors import PublishError\ns=State(Path(__import__('sys').argv[1]))\ntry:\n with s.lock('test'): raise SystemExit(99)\nexcept PublishError as e: raise SystemExit(e.exit_code)"
    with setup.lock("test"):
        child = subprocess.run([sys.executable, "-c", code, str(setup.root)], capture_output=True)
    assert child.returncode == 3, child.stderr.decode()


def test_account_cannot_rebind_or_bypass_journal(setup):
    with pytest.raises(PublishError):
        setup.bind("test", "other")
    with pytest.raises(PublishError):
        setup.bind("another", "test")


async def test_success_cached_without_media_or_browser(setup, tmp_path):
    request = args(tmp_path)
    output, code = await publish(setup, request, fake_session, Adapter)
    assert code == 0 and output["status"] == "published" and Adapter.clicks == 1
    Path(request.video).unlink()
    async def forbidden(*_):
        raise AssertionError("Cached result must not launch a browser")
    second, code = await publish(setup, request, forbidden, Adapter)
    assert code == 0 and second == output and Adapter.clicks == 1


@pytest.mark.parametrize("fail", [False, True])
async def test_acceptance_uses_snapshot_without_post_or_journal(setup, tmp_path, fail):
    request = args(tmp_path)
    snapshots = []
    class AcceptanceAdapter(Adapter):
        async def prepare(self, video, *_):
            snapshots.append(video)
            Path(request.video).write_bytes(b"changed source")
            assert video.read_bytes() == b"first video"

        async def verify(self, *_):
            if fail:
                raise PublishError("PRIVACY_MISMATCH", "Wrong audience")

        async def screenshot(self, *_):
            return None

        async def submit(self):
            raise AssertionError("Acceptance must never Post")

        async def confirm(self, *_):
            raise AssertionError("Acceptance must never confirm a submission")

    if fail:
        with pytest.raises(PublishError) as error:
            await accept_ui(setup, request, fake_session, AcceptanceAdapter)
        assert error.value.code == "PRIVACY_MISMATCH"
    else:
        result, code = await accept_ui(setup, request, fake_session, AcceptanceAdapter)
        assert code == 0 and result["status"] == "accepted" and result["submitted"] is False
        assert result["privacy"] == "everyone"
    with setup.connect() as db:
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    assert not snapshots[0].exists() and Adapter.clicks == 0


@pytest.mark.parametrize("failure", ["submit", "confirm"])
async def test_uncertain_never_submits_twice(setup, tmp_path, failure):
    Adapter.fail = failure
    request = args(tmp_path)
    output, code = await publish(setup, request, fake_session, Adapter)
    assert code == 4 and output["status"] == "uncertain" and Adapter.clicks == 1
    Path(request.video).unlink()
    again, code = await publish(setup, request, fake_session, Adapter)
    assert code == 4 and again == output and Adapter.clicks == 1
    if failure == "confirm":
        assert output["post_url"].endswith("123")


async def test_failed_before_boundary_and_changed_bytes_conflict(setup, tmp_path):
    Adapter.fail = "verify"
    request = args(tmp_path)
    output, code = await publish(setup, request, fake_session, Adapter)
    assert code == 2 and output["status"] == "failed" and Adapter.clicks == 0
    Path(request.video).write_bytes(b"different bytes")
    with pytest.raises(PublishError, match="different caption or media"):
        await publish(setup, request, fake_session, Adapter)
    assert Adapter.clicks == 0


async def test_failed_can_retry_same_fingerprint(setup, tmp_path):
    Adapter.fail = "verify"
    request = args(tmp_path)
    await publish(setup, request, fake_session, Adapter)
    Adapter.fail = None
    output, code = await publish(setup, request, fake_session, Adapter)
    assert code == 0 and output["status"] == "published" and Adapter.clicks == 1


@pytest.mark.parametrize("changes", [{"privacy": "friends"}, {"ai_generated": False}, {"cover": "different.png"}])
async def test_arguments_conflict_before_browser(setup, tmp_path, changes):
    request = args(tmp_path)
    await publish(setup, request, fake_session, Adapter)
    for name, value in changes.items():
        setattr(request, name, value)
    with pytest.raises(PublishError, match="different arguments"):
        await publish(setup, request, fake_session, Adapter)
    assert Adapter.clicks == 1


async def test_crash_boundary_recovers_to_uncertain(setup, tmp_path):
    request = args(tmp_path)
    setup.start("test", "one", normalized(request), "fingerprint", {"caption": ""})
    setup.finish("test", "one", "submitting")
    output, code = await publish(setup, request, fake_session, Adapter)
    assert code == 4 and output["error"]["code"] == "INTERRUPTED_SUBMISSION"
    assert setup.get("test", "one")["status"] == "uncertain" and Adapter.clicks == 0


async def test_journal_failure_after_click_is_uncertain(setup, tmp_path, monkeypatch):
    original = setup.finish
    def broken_finish(account, request, status, *args, **kwargs):
        if status != "submitting":
            raise OSError("disk full")
        return original(account, request, status, *args, **kwargs)
    monkeypatch.setattr(setup, "finish", broken_finish)
    output, code = await publish(setup, args(tmp_path), fake_session, Adapter)
    assert code == 4 and output["status"] == "uncertain" and Adapter.clicks == 1
    assert setup.get("test", "one")["status"] == "submitting"


async def test_reconcile_checks_saved_post_without_another_upload(setup, tmp_path, monkeypatch):
    import tiktok_automation.service as service
    Adapter.fail = "confirm"
    request = args(tmp_path)
    await publish(setup, request, fake_session, Adapter)
    Path(request.video).unlink()
    async def verify(*_):
        pass
    monkeypatch.setattr(service, "verify_account", verify)
    class ReconcileAdapter(Adapter):
        async def verify_post(self, url, handle, caption, privacy):
            assert url.endswith("/123") and handle == "test" and privacy == "everyone"
        async def prepare(self, *_):
            raise AssertionError("Reconcile must not upload")
        async def submit(self):
            raise AssertionError("Reconcile must not Post")
    output, code = await reconcile(setup, request, fake_session, ReconcileAdapter)
    assert code == 0 and output["status"] == "published" and Adapter.clicks == 1


def test_status_is_read_only(setup, tmp_path, capsys):
    request = args(tmp_path)
    setup.start("test", "one", normalized(request), "fingerprint", {})
    setup.finish("test", "one", "submitting")
    code = main(["status", "--state-dir", str(setup.root), "--account", "test", "--request-id", "one"])
    assert code == 0 and json.loads(capsys.readouterr().out)["status"] == "uncertain"
    assert setup.get("test", "one")["status"] == "submitting"


def test_login_timeout_exits_with_stdin_pipe_still_open(tmp_path):
    # A blocked executor readline would prevent asyncio.run from returning here.
    code = '''
import os, sys
from contextlib import asynccontextmanager
import tiktok_automation.service as service
from tiktok_automation.cli import main

class Page:
    async def goto(self, *args, **kwargs): pass
class Context:
    async def new_page(self): return Page()
@asynccontextmanager
async def session(*args): yield Context()
service.browser_session = session
reader, writer = os.pipe()
sys.stdin = os.fdopen(reader)
status = main(['login', '--account', 'test', '--state-dir', sys.argv[1], '--timeout-seconds', '1'])
os.close(writer)
raise SystemExit(status)
'''
    run = subprocess.run([sys.executable, "-c", code, str(tmp_path / "login-state")],
                         capture_output=True, text=True, timeout=3)
    assert run.returncode == 3, run.stderr
    assert len(run.stdout.splitlines()) == 1
    assert json.loads(run.stdout)["status"] == "failed"


async def test_stdin_readiness_handles_enter_and_eof():
    from tiktok_automation.service import wait_for_enter
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb", buffering=0) as stream:
        os.write(writer, b"\n")
        assert await wait_for_enter(stream) is True
        os.close(writer)
        assert await wait_for_enter(stream) is False


async def test_manual_reconcile_requires_explicit_video_comparison(setup, tmp_path):
    request = args(tmp_path)
    setup.start("test", "one", normalized(request), "fingerprint", {"caption": "Exact", "privacy": "everyone"})
    setup.finish("test", "one", "uncertain")
    request.post_url = "https://www.tiktok.com/@test/video/123"
    request.confirm_request_video = False
    with pytest.raises(PublishError) as exc:
        await reconcile(setup, request, fake_session, Adapter)
    assert exc.value.code == "MANUAL_CONFIRMATION_REQUIRED"
    assert setup.get("test", "one")["post_url"] is None and Adapter.clicks == 0


@pytest.mark.parametrize("failed_check", [False, True])
async def test_manual_reconcile_checks_one_url_without_upload(setup, tmp_path, monkeypatch, failed_check):
    import tiktok_automation.service as service
    request = args(tmp_path)
    setup.start("test", "one", normalized(request), "fingerprint", {"caption": "Exact", "privacy": "everyone"})
    setup.finish("test", "one", "uncertain")
    request.post_url = "https://www.tiktok.com/@test/video/123"
    request.confirm_request_video = True
    Path(request.video).unlink()
    async def verify(*_): pass
    monkeypatch.setattr(service, "verify_account", verify)
    class ManualAdapter:
        def __init__(self, *_): pass
        async def verify_post(self, url, handle, caption, privacy):
            assert (url, handle, caption, privacy) == (request.post_url, "test", "Exact", "everyone")
            if failed_check:
                raise PublishError("CONTENT_UNDER_REVIEW", "Reviewing", 4)
        async def prepare(self, *_): raise AssertionError("No manual reconcile upload")
        async def submit(self): raise AssertionError("No manual reconcile Post")
    output, code = await reconcile(setup, request, fake_session, ManualAdapter)
    assert code == (4 if failed_check else 0)
    assert output["status"] == ("uncertain" if failed_check else "published")
    assert output["post_url"] == (None if failed_check else request.post_url)
    assert Adapter.clicks == 0


async def test_manual_reconcile_rejects_wrong_account_and_saved_url_replacement(setup, tmp_path):
    request = args(tmp_path)
    setup.start("test", "one", normalized(request), "fingerprint", {"caption": "Exact", "privacy": "everyone"})
    setup.finish("test", "one", "uncertain", post_url="https://www.tiktok.com/@test/video/123")
    request.confirm_request_video = True
    request.post_url = "https://www.tiktok.com/@other/video/123"
    with pytest.raises(PublishError) as exc:
        await reconcile(setup, request, fake_session, Adapter)
    assert exc.value.code == "INVALID_POST_URL"
    request.post_url = "https://www.tiktok.com/@test/video/456"
    with pytest.raises(PublishError) as exc:
        await reconcile(setup, request, fake_session, Adapter)
    assert exc.value.code == "REQUEST_POST_CONFLICT"
    assert setup.get("test", "one")["post_url"].endswith("/123") and Adapter.clicks == 0
