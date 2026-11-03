"""Token authentication of the web server (alerts: unauthenticated read and write access)."""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from agentmon import auth, demo, sink  # noqa: E402
from agentmon.web import server  # noqa: E402

EVENT = {"type": "note", "run": "auth-run", "text": "hi"}


@pytest.fixture
def token() -> str:
    return auth.load_or_create()


@pytest.fixture
def anon() -> TestClient:
    return TestClient(server.create_app(), follow_redirects=False)


def _seed() -> str:
    evs = demo.build(until=20)
    for ev in evs:
        sink.append(ev)
    return evs[0]["run"]


ENDPOINTS = [("GET", "/"), ("GET", "/static/app.js"), ("GET", "/api/config"), ("GET", "/api/runs"),
             ("GET", "/api/runs/x/snapshot"), ("GET", "/api/runs/x/events.jsonl"), ("GET", "/api/stream"),
             ("POST", "/api/events")]


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_nothing_is_reachable_without_the_token(anon, method, path):
    res = anon.request(method, path, json=EVENT) if method == "POST" else anon.request(method, path)
    assert res.status_code == 401 and res.headers["www-authenticate"] == "Bearer"


def test_a_rejected_post_writes_nothing(anon):
    anon.post("/api/events", json=EVENT)
    assert not (auth.paths.run_file("auth-run")).exists()


@pytest.mark.parametrize("bad", ["", "wrong", "x" * 43, "Bearer", "Basic abc"])
def test_wrong_credentials_are_rejected(anon, token, bad):
    assert anon.get("/api/runs", headers={"Authorization": bad}).status_code == 401
    assert anon.get("/api/runs", cookies={"agentmon_token": bad}).status_code == 401
    assert anon.get(f"/api/runs?token={bad}").status_code == 401  # the query token only works on a GET that redirects


def test_bearer_token_grants_read_and_write(anon, token):
    h = {"Authorization": f"Bearer {token}"}
    assert anon.post("/api/events", json=EVENT, headers=h).json() == {"accepted": 1}
    assert anon.get("/api/runs", headers=h).status_code == 200
    assert anon.get("/api/runs", headers={"Authorization": f"bearer {token}"}).status_code == 200  # scheme is case-insensitive


def test_opening_the_url_with_the_token_sets_a_strict_httponly_cookie_and_hides_the_token(anon, token):
    res = anon.get(f"/?token={token}&run=r1")
    assert res.status_code == 303
    assert res.headers["location"] == "/?run=r1" and token not in res.headers["location"]
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and f"agentmon_token={token}".lower() in cookie
    follow = anon.get("/", cookies={"agentmon_token": token})
    assert follow.status_code == 200 and "<html" in follow.text.lower()


def test_redirect_never_leaves_the_site(anon, token):
    res = anon.get(f"http://testserver//evil.example/path?token={token}")  # a bare "//host/..." would be read as another host
    assert res.status_code == 303 and res.headers["location"] == "/evil.example/path"


def test_cookie_authenticates_the_event_stream(anon, token):
    run = _seed()
    with anon.stream("GET", f"/api/stream?run={run}", cookies={"agentmon_token": token}) as res:
        assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
        first = next(res.iter_lines())
        assert first.startswith("event: ") or first.startswith(":")


def test_token_is_stable_regenerated_when_damaged_and_never_empty():
    first = auth.load_or_create()
    assert len(first) >= auth.MIN_LENGTH and auth.load_or_create() == first
    auth.token_path().write_text("short")
    second = auth.load_or_create()
    assert second != "short" and len(second) >= auth.MIN_LENGTH and auth.token_path().read_text().strip() == second
    auth.token_path().unlink()
    assert len(auth.load_or_create()) >= auth.MIN_LENGTH


def test_each_install_gets_its_own_token(tmp_path, monkeypatch):
    first = auth.load_or_create()
    monkeypatch.setenv("AGENTMON_HOME", str(tmp_path / "other"))
    assert auth.load_or_create() != first


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_token_file_is_owner_only():
    import stat

    auth.load_or_create()
    assert stat.S_IMODE(auth.token_path().stat().st_mode) == 0o600


def test_explicit_token_overrides_the_stored_one():
    app = TestClient(server.create_app(token="t" * 40), headers={"Authorization": "Bearer " + "t" * 40})
    assert app.get("/api/runs").status_code == 200
    assert TestClient(server.create_app(token="t" * 40), headers={"Authorization": f"Bearer {auth.load_or_create()}"}).get("/api/runs").status_code == 401


def test_query_token_never_authorises_a_write(anon, token):
    assert anon.post(f"/api/events?token={token}", json=EVENT).status_code == 401
    assert not auth.paths.run_file("auth-run").exists()
