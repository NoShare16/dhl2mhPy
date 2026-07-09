import pytest
from fastapi.testclient import TestClient

from dhl2mh import web
from dhl2mh.config import Settings


def _web_settings(monkeypatch, *, username="boss", password="s3cret-long-pw") -> Settings:
    env = {
        "APP_ENV": "dev",
        "REPORT_RECIPIENT_EMAIL": "ops@example.com",
        "PLENTY__USERNAME": "u",
        "PLENTY__PASSWORD": "p",
        "SHOPWARE__CLIENT_ID": "cid",
        "SHOPWARE__CLIENT_SECRET": "csec",
        "DHL__UAT_USERNAME": "HDE",
        "DHL__UAT_PASSWORD": "uatpw",
        "DHL__PROD_USERNAME": "HDE",
        "DHL__PROD_PASSWORD": "prodpw",
        "SMTP__HOST": "smtp.test",
        "SMTP__USERNAME": "smtp-user",
        "SMTP__PASSWORD": "smtp-pw",
        "SMTP__FROM_EMAIL": "from@test.com",
        "WEB__USERNAME": username,
        "WEB__PASSWORD": password,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def client(monkeypatch) -> TestClient:
    settings = _web_settings(monkeypatch)
    monkeypatch.setattr(web, "get_settings", lambda: settings)
    # fresh run-state per test so the concurrency guard is deterministic
    monkeypatch.setattr(web, "_state", web.RunState())
    # never spawn a real subprocess in tests
    started: list[bool] = []

    async def fake_start() -> bool:
        if web._state.running:
            return False
        started.append(True)
        web._state.proc = object()  # type: ignore[assignment]
        return True

    monkeypatch.setattr(web, "_start_run", fake_start)
    c = TestClient(web.app, base_url="https://testserver")
    c._started = started  # type: ignore[attr-defined]
    return c


def test_status_unauthenticated(client):
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json() == {"authed": False}


def test_login_wrong_password_rejected(client):
    r = client.post("/login", json={"username": "boss", "password": "nope"})
    assert r.status_code == 401
    assert web.COOKIE_NAME not in r.cookies


def test_login_then_status_authed(client):
    r = client.post("/login", json={"username": "boss", "password": "s3cret-long-pw"})
    assert r.status_code == 200
    r2 = client.get("/status")
    assert r2.json()["authed"] is True


def test_trigger_requires_auth(client):
    r = client.post("/trigger", json={})
    assert r.status_code == 401
    assert client._started == []


def test_trigger_starts_run_once_and_blocks_second(client):
    client.post("/login", json={"username": "boss", "password": "s3cret-long-pw"})
    r1 = client.post("/trigger", json={})
    assert r1.status_code == 200
    r2 = client.post("/trigger", json={})  # one already "running"
    assert r2.status_code == 409
    assert client._started == [True]


def test_disabled_when_no_credentials(monkeypatch):
    settings = _web_settings(monkeypatch, username="", password="")
    monkeypatch.setattr(web, "get_settings", lambda: settings)
    c = TestClient(web.app, base_url="https://testserver")
    assert c.get("/status").json() == {"authed": False, "disabled": True}
    assert c.post("/login", json={"username": "", "password": ""}).status_code == 503