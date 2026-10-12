import json
import os
import time

import httpx
import pytest
from app_helpers import flow_app as create_app

from werewolf.accounts import AccountStore, password_hash, password_matches, token_hash
from werewolf.service import ServiceError

PASSWORD = "local-test-password-123"
KEY = "fake-offline-api-key-only"


def input_connection(**extra):
    return {"label": "本机测试连接", "provider": "siliconflow", "model": "offline-model-id",
            "generation": {"temperature": 0.7, "max_output_tokens": 768, "target_output_tokens": 256,
                           "reasoning_budget": None}, "api_key": KEY, **extra}


async def register(client, username="alice"):
    result = await client.post("/api/account/register", json={"username": username, "password": PASSWORD})
    assert result.status_code == 201, result.text
    return result.json(), {"X-CSRF-Token": result.json()["csrf_token"]}


@pytest.mark.asyncio
async def test_login_cookie_logout_rotation_and_no_game_side_effects(tmp_path):
    app = create_app(tmp_path / "game.sqlite3")
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/account/status")).json()["user"] is None
        assert (await client.get("/api/account/connections")).status_code == 401
        result = await client.post("/api/account/register", json={"username": "Alice", "password": PASSWORD})
        assert result.status_code == 201
        assert result.json()["user"]["username"] == "alice"
        assert PASSWORD not in result.text
        assert "HttpOnly" in result.headers["set-cookie"] and "SameSite=strict" in result.headers["set-cookie"]
        assert "Path=/api/account" in result.headers["set-cookie"]
        assert result.headers["cache-control"] == "no-store"
        original_cookie = client.cookies.get("werewolf_account")
        assert original_cookie not in result.text
        csrf = {"X-CSRF-Token": result.json()["csrf_token"]}
        assert (await client.post("/api/account/logout", json={})).status_code == 403
        assert (await client.post("/api/account/logout", json={},
                                  headers=[(b"x-csrf-token", b"\xff")])).status_code == 403
        assert (await client.post("/api/account/logout", json={}, headers=csrf)).status_code == 200
        client.cookies.set("werewolf_account", original_cookie, domain="localhost.local", path="/api/account")
        assert (await client.get("/api/account/status")).json()["user"] is None
        bad = await client.post("/api/account/login", json={"username": "alice", "password": "wrong-password-123"})
        assert bad.status_code == 401
        logged = await client.post("/api/account/login", json={"username": "ALICE", "password": PASSWORD})
        assert logged.status_code == 200
        assert logged.json()["csrf_token"] != csrf["X-CSRF-Token"]
        assert client.cookies.get("werewolf_account") != original_cookie
        assert app.state.service.games == {}


@pytest.mark.asyncio
async def test_connection_crud_encryption_and_cross_user_denial(tmp_path):
    app = create_app(tmp_path / "game.sqlite3")
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        alice, alice_csrf = await register(client)
        created = await client.post("/api/account/connections", json=input_connection(), headers=alice_csrf)
        assert created.status_code == 201, created.text
        connection_id = created.json()["id"]
        assert KEY not in created.text and created.json()["key_mask"] == "••••••••"
        assert created.json()["verification"] == "not_verified"
        vault = app.state.accounts
        row = vault.db.execute("SELECT config,encrypted_key FROM connections").fetchone()
        assert KEY not in row["config"] and KEY.encode() not in row["encrypted_key"]
        assert PASSWORD not in vault.db.execute("SELECT password_hash FROM users").fetchone()[0]
        assert vault.credential(alice["user"]["id"], connection_id) == KEY
        preserved = await client.post(f"/api/account/connections/{connection_id}",
                                     json=input_connection(api_key=None, label="已改名"), headers=alice_csrf)
        assert preserved.status_code == 200 and preserved.json()["label"] == "已改名"
        assert vault.credential(alice["user"]["id"], connection_id) == KEY
        switched = await client.post(f"/api/account/connections/{connection_id}",
                                    json=input_connection(api_key=None, provider="deepseek"), headers=alice_csrf)
        assert switched.status_code == 422
        replacement = "replacement-fake-test-key"
        assert (await client.post(f"/api/account/connections/{connection_id}",
                                  json=input_connection(api_key=replacement), headers=alice_csrf)).status_code == 200
        assert vault.credential(alice["user"]["id"], connection_id) == replacement
        bob, bob_csrf = await register(client, "bob")
        assert (await client.get("/api/account/connections")).json() == {"connections": []}
        for suffix, body in [("", input_connection()), ("/delete", {"confirm": True})]:
            assert (await client.post(f"/api/account/connections/{connection_id}{suffix}",
                                      json=body, headers=bob_csrf)).status_code == 404
        with pytest.raises(ServiceError, match="连接不存在"):
            vault.credential(bob["user"]["id"], connection_id)
        login = (await client.post("/api/account/login", json={"username": "alice", "password": PASSWORD})).json()
        assert len((await client.get("/api/account/connections")).json()["connections"]) == 1
        assert (await client.post(f"/api/account/connections/{connection_id}/delete", json={"confirm": False},
                                  headers={"X-CSRF-Token": login["csrf_token"]})).status_code == 422
        assert (await client.post(f"/api/account/connections/{connection_id}/delete", json={"confirm": True},
                                  headers={"X-CSRF-Token": login["csrf_token"]})).status_code == 200
        assert (await client.get("/api/account/connections")).json() == {"connections": []}
        assert app.state.service.games == {}


@pytest.mark.asyncio
async def test_restart_restores_account_settings_and_session(tmp_path):
    path = tmp_path / "game.sqlite3"
    app = create_app(path)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        alice, headers = await register(client)
        connection = (await client.post("/api/account/connections", json=input_connection(), headers=headers)).json()
        cookies = client.cookies
    resumed = create_app(path)
    async with resumed.router.lifespan_context(resumed), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=resumed), base_url="http://localhost", cookies=cookies
    ) as client:
        assert (await client.get("/api/account/status")).json() == alice
        assert (await client.get("/api/account/connections")).json()["connections"] == [connection]
        assert resumed.state.accounts.credential(alice["user"]["id"], connection["id"]) == KEY


@pytest.mark.asyncio
async def test_password_change_revokes_all_other_sessions_but_preserves_keys(tmp_path):
    app = create_app(tmp_path / "game.sqlite3")
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        user, headers = await register(client)
        await client.post("/api/account/connections", json=input_connection(), headers=headers)
        old_cookie = client.cookies.get("werewolf_account")
        bad = await client.post("/api/account/password", json={"old_password": "wrong-password-123", "new_password": PASSWORD}, headers=headers)
        assert bad.status_code == 400
        new = await client.post("/api/account/password", json={"old_password": PASSWORD, "new_password": "new-long-password-123"}, headers=headers)
        assert new.status_code == 200
        assert client.cookies.get("werewolf_account") != old_cookie
        assert app.state.accounts.session(old_cookie) is None
        assert len((await client.get("/api/account/connections")).json()["connections"]) == 1
        assert (await client.post("/api/account/login", json={"username": "alice", "password": PASSWORD})).status_code == 401
        assert (await client.post("/api/account/login", json={"username": "alice", "password": "new-long-password-123"})).status_code == 200
        assert user["user"] == new.json()["user"]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"},
    {"Host": "evil.example"}, {"Content-Type": "text/plain"},
])
async def test_account_rejects_cross_site_host_and_non_json(tmp_path, headers):
    app = create_app(tmp_path / "game.sqlite3")
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post("/api/account/register", content=json.dumps({"username": "alice", "password": PASSWORD}),
                                   headers={"Content-Type": "application/json", **headers})
        assert result.status_code in {403, 415, 422}
        assert app.state.accounts.db.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        assert PASSWORD not in result.text


@pytest.mark.asyncio
async def test_account_validation_never_echoes_credentials(tmp_path):
    app = create_app(tmp_path / "game.sqlite3")
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post("/api/account/register", json={"username": "??", "password": "raw-secret"})
        assert result.status_code == 422 and "raw-secret" not in result.text
        _, headers = await register(client)
        for body in [input_connection(api_key="fake key secret"), input_connection(base_url="http://evil.example"),
                     input_connection(generation={"temperature": 7.0}), input_connection(api_key=None)]:
            response = await client.post("/api/account/connections", json=body, headers=headers)
            assert response.status_code == 422 and "fake key secret" not in response.text
            assert response.headers["cache-control"] == "no-store"


def test_vault_files_private_lost_wrong_key_fail_closed_and_cipher_owner_check(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    vault = AccountStore(path)
    user, token = vault.register("alice", PASSWORD, "local")
    saved = vault.save_connection(user["user"]["id"], None, input_connection(api_key=None), KEY)
    assert path.stat().st_mode & 0o777 == vault.key_path.stat().st_mode & 0o777 == 0o600
    assert vault.db.execute("SELECT token_hash FROM sessions").fetchone()[0] == token_hash(token)
    with vault.db:
        vault.db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
        vault.db.execute("UPDATE connections SET encrypted_key=? WHERE id=?",
                         (vault.cipher.encrypt(json.dumps({"user_id": "wrong-user", "api_key": KEY}).encode()), saved["id"]))
    assert vault.session(token) is None
    with pytest.raises(ServiceError, match="无法解密"):
        vault.credential(user["user"]["id"], saved["id"])
    original_key = vault.key_path.read_bytes()
    vault.close()
    vault.key_path.unlink()
    with pytest.raises(ValueError, match="密钥缺失"):
        AccountStore(path)
    assert not vault.key_path.exists()
    from cryptography.fernet import Fernet
    vault.key_path.write_bytes(Fernet.generate_key())
    os.chmod(vault.key_path, 0o600)
    with pytest.raises(ValueError, match="不匹配"):
        AccountStore(path)
    vault.key_path.write_bytes(original_key)
    restored = AccountStore(path)
    restored.close()
    os.chmod(vault.key_path, 0o644)
    with pytest.raises(ValueError, match="本人可读写"):
        AccountStore(path)


def test_password_hashes_salted_slow_and_rate_limit(tmp_path):
    first, second = password_hash(PASSWORD), password_hash(PASSWORD)
    assert first != second and PASSWORD not in first
    assert password_matches(PASSWORD, first) and not password_matches("wrong", first)
    vault = AccountStore(tmp_path / "accounts.sqlite3")
    for _ in range(10):
        vault.throttle("local")
    with pytest.raises(ServiceError) as result:
        vault.throttle("local")
    assert result.value.status == 429
    vault.close()
