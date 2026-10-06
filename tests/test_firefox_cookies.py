from argparse import Namespace
from contextlib import asynccontextmanager
import sqlite3
import time

import pytest

from tiktok_automation.errors import PublishError
from tiktok_automation.firefox_cookies import firefox_cookies
from tiktok_automation.service import login
from tiktok_automation.state import State


def profile_db(tmp_path, version=17):
    profile = tmp_path / "firefox"
    profile.mkdir()
    db = sqlite3.connect(profile / "cookies.sqlite")
    db.execute(f"PRAGMA user_version={version}")
    db.execute("""CREATE TABLE moz_cookies (
        name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER,
        isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER, originAttributes TEXT
    )""")
    return profile, db


@pytest.mark.parametrize("version,scale", [(15, 1), (16, 1000), (17, 1000)])
def test_snapshot_preserves_site_host_scope_and_attributes(tmp_path, version, scale):
    profile, db = profile_db(tmp_path, version)
    future = int((time.time() + 86400) * scale)
    rows = [
        ("sessionid", "secret-a", ".tiktok.com", "/", future, 1, 1, 2, ""),
        ("host-only", "secret-b", "www.tiktok.com", "/", future, 1, 1, 256, ""),
        ("other", "other-secret", "facebook.com", "/", future, 1, 0, 1, ""),
        ("spoof", "other-secret", "eviltiktok.com", "/", future, 1, 0, 1, ""),
        ("container", "secret", ".tiktok.com", "/", future, 1, 0, 1, "^userContextId=2"),
        ("partition", "secret", ".tiktok.com", "/", future, 1, 0, 1, "^partitionKey=%28https%2Cexample.com%29"),
        ("missing-scope", "secret", ".tiktok.com", "/", future, 1, 0, 1, None),
        ("expired", "secret", ".tiktok.com", "/", 1, 1, 1, 0, ""),
    ]
    db.executemany("INSERT INTO moz_cookies VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    cookies = firefox_cookies(profile)
    assert {cookie["name"] for cookie in cookies} == {"sessionid", "host-only"}
    assert cookies[0] == {"name": "sessionid", "value": "secret-a", "domain": ".tiktok.com",
                          "path": "/", "expires": future / scale, "secure": True, "httpOnly": True, "sameSite": "Strict"}
    assert cookies[1]["domain"] == "www.tiktok.com" and "url" not in cookies[1] and "sameSite" not in cookies[1]
    with sqlite3.connect(profile / "cookies.sqlite") as original:
        assert original.execute("SELECT count(*) FROM moz_cookies").fetchone()[0] == len(rows)


def test_unknown_schema_and_missing_session_do_not_expose_values(tmp_path):
    profile, db = profile_db(tmp_path, 18)
    db.close()
    with pytest.raises(PublishError) as error:
        firefox_cookies(profile)
    assert error.value.code == "FIREFOX_SCHEMA_UNSUPPORTED"
    with sqlite3.connect(profile / "cookies.sqlite") as db:
        db.execute("PRAGMA user_version=17")
        db.execute("INSERT INTO moz_cookies VALUES (?,?,?,?,?,?,?,?,?)", (
            "sessionid", "secret", ".tiktok.com", "/", int((time.time() + 86400) * 1000), 1, 1, 1, "^userContextId=2"))
    with pytest.raises(PublishError) as error:
        firefox_cookies(profile)
    assert error.value.code == "FIREFOX_SESSION_MISSING" and "secret" not in error.value.message


@pytest.mark.parametrize("identity", ["caribbeanawesome", "other"])
async def test_import_binds_only_camoufox_verified_expected_identity(tmp_path, monkeypatch, identity):
    import tiktok_automation.service as service
    state = State(tmp_path / "state")
    args = Namespace(account="caribbean", handle="@CaribbeanAwesome", import_firefox=tmp_path / "firefox", timeout_seconds=30)
    cookies = [{"name": "sessionid", "value": "secret", "domain": ".tiktok.com", "path": "/"}]
    monkeypatch.setattr(service, "firefox_cookies", lambda _: cookies)
    calls = []

    class Context:
        async def add_cookies(self, values):
            calls.append(("add", values))

        async def clear_cookies(self):
            calls.append(("clear", None))

    @asynccontextmanager
    async def session(profile, _):
        (profile / "cookies.sqlite").write_text("private session")
        yield Context()

    async def current(context):
        assert calls == [("add", cookies)]
        return identity

    monkeypatch.setattr(service, "current_handle", current)
    if identity == "caribbeanawesome":
        result, code = await login(state, args, session)
        assert code == 0 and result["status"] == "authenticated"
        assert state.account("caribbean")["handle"] == identity
    else:
        with pytest.raises(PublishError) as error:
            await login(state, args, session)
        assert error.value.code == "ACCOUNT_MISMATCH" and calls[-1] == ("clear", None)
        assert not state.accounts()
        assert not any(state.profile("caribbean").iterdir())


async def test_import_requires_expected_handle_and_unused_profile(tmp_path, monkeypatch):
    import tiktok_automation.service as service
    state = State(tmp_path / "state")
    args = Namespace(account="caribbean", handle=None, import_firefox=tmp_path / "firefox", timeout_seconds=30)
    def forbidden(_):
        raise AssertionError("Unsafe import must not read Firefox cookies")
    monkeypatch.setattr(service, "firefox_cookies", forbidden)
    with pytest.raises(PublishError) as error:
        await login(state, args)
    assert error.value.code == "EXPECTED_HANDLE_REQUIRED"
    args.handle = "caribbeanawesome"
    marker = state.profile("caribbean") / "existing-profile"
    marker.write_text("preserved")
    with pytest.raises(PublishError) as error:
        await login(state, args)
    assert error.value.code == "FIREFOX_IMPORT_CONFLICT" and marker.read_text() == "preserved"
