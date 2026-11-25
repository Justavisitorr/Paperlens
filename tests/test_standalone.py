from __future__ import annotations

import json
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from paperlens.account_store import AccountStore
from paperlens.standalone import create_standalone
from paperlens.standalone_config import StandaloneConfig

ORIGIN = "https://research.example"


@pytest.fixture
def setup(tmp_path):
    mail = {}

    def sender(config, challenge, email, code):
        mail[challenge] = (email, code)

    config = StandaloneConfig(tmp_path / "data", ORIGIN, smtp_host="smtp.example", smtp_from="noreply@example.com")
    app = create_standalone(config, mail_sender=sender)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, app.state.standalone, mail, config


def login(client, mail, email="alice@example.com"):
    client.get("/login")
    reply = client.post("/auth/email/request", json={"email": email}, headers={"Origin": ORIGIN})
    assert reply.status_code == 200, reply.text
    ident = reply.json()["challenge"]
    assert "code" not in reply.json()
    reply = client.post("/auth/email/verify", json={"challenge": ident, "code": mail[ident][1]}, headers={"Origin": ORIGIN})
    assert reply.status_code == 200, reply.text
    info = client.get("/auth/session").json()
    return {"Origin": ORIGIN, "X-PaperLens-CSRF": info["csrf"], "X-PaperLens-Workspace": info["workspace"]}


def test_login_isolation_keys_and_restart(setup, monkeypatch):
    client, service, mail, config = setup
    monkeypatch.setenv("LLM_API_KEY", "administrator-secret")
    assert client.get("/api/library").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    alice = login(client, mail)
    cookie = client.cookies.get(service.session_cookie)
    assert cookie
    assert "administrator-secret" not in client.get("/api/settings", headers=alice).text
    result = client.post("/auth/data", headers=alice, json={"changes": [{"key": "paperlens.terms", "value": '[{"term":"attention"}]', "version": 0}]})
    assert result.status_code == 200, result.text
    assert result.json()["accepted"]["paperlens.terms"]["version"] == 1
    reply = client.post("/api/connections/accounts", headers=alice, json={"provider_id":"deepseek", "name":"Alice", "api_key":"alice-private-key"})
    assert reply.status_code == 201, reply.text
    alice_workspace = service.config.root / "workspaces" / alice["X-PaperLens-Workspace"]
    assert "alice-private-key" not in (alice_workspace / ".paperlens/connections.json").read_text("utf-8")
    client.cookies.clear()
    bob = login(client, mail, "bob@example.com")
    assert client.get("/auth/data", headers=bob).json()["items"] == {}
    assert "Alice" not in client.get("/api/connections", headers=bob).text
    assert client.get("/auth/data", headers=alice).status_code == 409
    assert client.post("/auth/data", headers=alice, json={"changes":[]}).status_code == 403
    store = AccountStore(config.root / "accounts.sqlite3", service.service.vault_key)
    alice_user = store.user(cookie)
    assert alice_user and store.snapshot(alice_user["id"], service.policy(alice_user["id"]))["paperlens.terms"]["value"].startswith("[")
    assert b"attention" not in (config.root / "accounts.sqlite3").read_bytes()


def test_csrf_expiry_attempts_and_replay(setup):
    client, service, mail, _ = setup
    client.get("/login")
    assert client.post("/auth/email/request", json={"email":"a@example.com"}).status_code == 403
    reply = client.post("/auth/email/request", headers={"Origin":ORIGIN}, json={"email":"a@example.com"})
    ident = reply.json()["challenge"]
    wrong = "000000" if mail[ident][1] != "000000" else "111111"
    for _ in range(5):
        assert client.post("/auth/email/verify", headers={"Origin":ORIGIN}, json={"challenge":ident,"code":wrong}).status_code == 400
    assert client.post("/auth/email/verify", headers={"Origin":ORIGIN}, json={"challenge":ident,"code":mail[ident][1]}).status_code == 400
    headers = login(client, mail, "valid@example.com")
    valid_id = next(k for k, v in mail.items() if v[0] == "valid@example.com")
    assert client.post("/auth/email/verify", headers=headers, json={"challenge":valid_id,"code":mail[valid_id][1]}).status_code == 400
    assert client.post("/auth/logout", headers={**headers,"Origin":"https://evil.example"}).status_code == 403
    token = client.cookies.get(service.session_cookie)
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert service.store.user(token) is None
    ident, code = service.store.challenge("expired@example.com", "browser")
    with service.store.db() as db:
        db.execute("UPDATE challenges SET expires=? WHERE id=?", (time.time()-1, ident))
    with pytest.raises(Exception, match="400"):
        service.store.verify(ident, code, "browser")


def test_sync_conflict_delete_quota_and_secret_keys(setup):
    client, _, mail, _ = setup
    headers = login(client, mail)

    def sync(value, version, key="paperlens.terms"):
        return client.post("/auth/data", headers=headers, json={"changes":[{"key":key,"value":value,"version":version}]})

    assert sync("device A", 0).status_code == 200
    conflict = sync("device B", 0).json()
    assert conflict["conflicts"]["paperlens.terms"] == {"value":"device A", "version":1}
    assert client.get("/auth/data", headers=headers).json()["items"]["paperlens.terms"]["value"] == "device A"
    assert sync("device B", 1).json()["accepted"]["paperlens.terms"]["version"] == 2
    assert sync(None, 2).status_code == 200
    assert sync("old offline tab", 2).json()["conflicts"]["paperlens.terms"]["value"] is None
    assert sync("secret", 0, "LLM_API_KEY").status_code == 422
    assert sync("x" * (1024**2 + 1), 0).status_code == 422
    assert client.get("/auth/data").status_code == 409


def test_github_pkce_link_and_no_email_auto_merge(tmp_path):
    captured = []

    def provider(request):
        captured.append(request)
        if request.url.path.endswith("access_token"):
            return httpx.Response(200, json={"access_token":"test-token", "token_type":"bearer", "scope":"read:user"})
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"id":123, "login":"alice-github", "email":"alice@example.com"})

    config = StandaloneConfig(tmp_path, ORIGIN, github_id="test-client", github_secret="test-secret")
    app = create_standalone(config, oauth_transport=httpx.MockTransport(provider))
    service = app.state.standalone
    email_uid = service.store.identity("email", "alice@example.com", "Alice")
    with TestClient(app, base_url=ORIGIN) as client:
        client.get("/login")
        reply = client.post("/auth/github/start", headers={"Origin": ORIGIN})
        query = parse_qs(urlsplit(reply.json()["url"]).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["scope"] == ["read:user"]
        assert client.get("/auth/github/callback?state=wrong&code=test").status_code == 400
        callback = "/auth/github/callback?state=" + query["state"][0] + "&code=test"
        response = client.get(callback, follow_redirects=False)
        assert response.status_code == 303, response.text
        assert "HttpOnly" in response.headers["set-cookie"] and "Secure" in response.headers["set-cookie"]
        assert client.get("/auth/session").json()["user"]["id"] != email_uid
        assert "code_verifier=" in captured[0].content.decode()
        assert client.get(callback).status_code == 400
        assert b"test-token" not in (tmp_path / "accounts.sqlite3").read_bytes()


def test_invalid_production_config_and_missing_key(tmp_path):
    with pytest.raises(ValueError):
        StandaloneConfig(tmp_path, "http://public.example")
    with pytest.raises(ValueError):
        StandaloneConfig(tmp_path, ORIGIN, dev_mail=True)
    with pytest.raises(ValueError):
        StandaloneConfig(tmp_path, ORIGIN, github_id="only-one-half")
    with pytest.raises(ValueError):
        StandaloneConfig(tmp_path, ORIGIN, trusted_proxies="*")
    (tmp_path / "accounts.sqlite3").touch()
    with pytest.raises(RuntimeError, match="account.key"):
        StandaloneConfig(tmp_path, ORIGIN).service()


def test_explicit_config_file_and_relative_secret(tmp_path, monkeypatch):
    for name in ("PAPERLENS_SMTP_PASSWORD", "PAPERLENS_SMTP_PASSWORD_FILE", "PAPERLENS_SMTP_HOST"):
        monkeypatch.delenv(name, raising=False)
    config_file = tmp_path / ".env.standalone"
    config_file.write_text("PAPERLENS_SMTP_HOST=smtp.example\nPAPERLENS_SMTP_PASSWORD_FILE=mail-secret\nLLM_API_KEY=not-inherited\n", encoding="utf-8")
    (tmp_path / "mail-secret").write_text("private-password", encoding="utf-8")
    config = StandaloneConfig.from_env(tmp_path / "data", ORIGIN, env_file=config_file)
    assert config.smtp_host == "smtp.example"
    assert config.smtp_password == "private-password"
    assert "private-password" not in repr(config)


def test_email_not_configured_no_test_code_exposure(tmp_path):
    config = StandaloneConfig(tmp_path, "http://127.0.0.1:8080", dev_mail=True)
    app = create_standalone(config)
    with TestClient(app, base_url=config.origin) as client:
        client.get("/login")
        result = client.post("/auth/email/request", headers={"Origin": config.origin}, json={"email":"demo@example.com"})
        saved = json.loads((tmp_path / "test-mail" / (result.json()["challenge"] + ".json")).read_text())
        assert saved["code"] not in result.text
        assert client.get("/test-mail/" + result.json()["challenge"] + ".json").status_code == 401
    config = replace(config, dev_mail=False)
    with TestClient(create_standalone(config), base_url=config.origin) as client:
        client.get("/login")
        assert client.post("/auth/email/request", headers={"Origin":config.origin}, json={"email":"demo@example.com"}).status_code == 503
