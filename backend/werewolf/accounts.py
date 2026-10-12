"""Local account vault. Never include secrets in game snapshots or public views."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .profiles import canonical_json
from .service import ServiceError

SESSION_SECONDS = 7 * 24 * 3600
PROVIDER_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
}


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    # OWASP scrypt baseline. One hash at a time under the vault lock.
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**17, r=8, p=1,
                            maxmem=256 * 1024 * 1024, dklen=32)
    return f"scrypt-v1${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    _, salt, _ = encoded.split("$")
    return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)), encoded)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def private_file(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if create else 0)
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("账号库和密钥文件必须属于当前用户，且仅本人可读写")
    return fd


class AccountStore:
    def __init__(self, path: Path):
        self.path = path
        self.key_path = path.with_suffix(".key")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.limits: dict[str, list[float]] = {}
        # Never replace a lost key for an existing database: fail closed.
        if path.exists() and not self.key_path.exists():
            raise ValueError("账号库的加密密钥缺失，请恢复原密钥；不会自动覆盖生成")
        try:
            fd = private_file(self.key_path, create=True)
        except FileExistsError:
            fd = private_file(self.key_path, create=False)
            with os.fdopen(fd, "rb") as file:
                key = file.read(128)
        else:
            key = Fernet.generate_key()
            with os.fdopen(fd, "wb") as file:
                file.write(key)
                file.flush()
                os.fsync(file.fileno())
        try:
            self.cipher = Fernet(key)
        except ValueError as exc:
            raise ValueError("账号库加密密钥无效，不会重新生成") from exc
        try:
            fd = private_file(path, create=True)
        except FileExistsError:
            fd = private_file(path, create=False)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        # DELETE journal avoids leaving credential-bearing WAL files across restarts.
        self.db.execute("PRAGMA journal_mode=DELETE")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            self.db.close()
            raise ValueError("不支持的账号库版本")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                csrf TEXT NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connections (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                config TEXT NOT NULL, encrypted_key BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vault_meta (id INTEGER PRIMARY KEY CHECK(id=1), probe BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS table_plans (
                user_id TEXT PRIMARY KEY REFERENCES users(id), version TEXT NOT NULL, payload TEXT NOT NULL
            );
            PRAGMA user_version=1;
        """)
        probe = self.db.execute("SELECT probe FROM vault_meta WHERE id=1").fetchone()
        try:
            if probe is not None:
                if self.cipher.decrypt(probe[0]) != b"werewolf-account-v1":
                    raise InvalidToken
            else:
                with self.db:
                    self.db.execute("INSERT INTO vault_meta VALUES(1,?)",
                                    (self.cipher.encrypt(b"werewolf-account-v1"),))
        except InvalidToken as exc:
            self.db.close()
            raise ValueError("加密密钥与账号库不匹配，请恢复原密钥") from exc
        # Fixed dummy hash removes the cheap nonexistent-user login path.
        self.dummy_hash = f"scrypt-v1${'00' * 16}${'00' * 32}"

    def throttle(self, peer: str) -> None:
        now = time.monotonic()
        self.limits = {k: [t for t in v if now - t < 60] for k, v in self.limits.items()
                       if v and now - v[-1] < 60}
        attempts = self.limits.setdefault(peer, [])
        if len(attempts) >= 10 or len(self.limits) > 1024:
            raise ServiceError(429, "操作太频繁，请稍等一分钟再试")
        attempts.append(now)

    def new_session(self, user_id: str) -> tuple[dict, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            self.db.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                            (token_hash(token), user_id, csrf, time.time() + SESSION_SECONDS))
        username = self.db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()[0]
        return {"user": {"id": user_id, "username": username}, "csrf_token": csrf}, token

    def register(self, username: str, password: str, peer: str) -> tuple[dict, str]:
        with self.lock:
            self.throttle(peer)
            encoded = password_hash(password)
            user_id = uuid.uuid4().hex
            try:
                with self.db:
                    self.db.execute("INSERT INTO users VALUES(?,?,?)", (user_id, username, encoded))
            except sqlite3.IntegrityError as exc:
                raise ServiceError(409, "用户名已被使用") from exc
            return self.new_session(user_id)

    def login(self, username: str, password: str, peer: str) -> tuple[dict, str]:
        with self.lock:
            self.throttle(peer)
            row = self.db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            valid = password_matches(password, row["password_hash"] if row else self.dummy_hash)
            if not valid or row is None:
                raise ServiceError(401, "用户名或密码不正确")
            return self.new_session(row["id"])

    def session(self, token: str | None) -> dict | None:
        with self.lock:
            if not token or len(token) > 128:
                return None
            row = self.db.execute(
                "SELECT users.id,username,csrf FROM sessions JOIN users ON users.id=sessions.user_id "
                "WHERE token_hash=? AND expires>?", (token_hash(token), time.time())
            ).fetchone()
            return {"user": {"id": row["id"], "username": row["username"]}, "csrf_token": row["csrf"]} if row else None

    def logout(self, token: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))

    def change_password(self, user_id: str, old: str, new: str, peer: str) -> tuple[dict, str]:
        with self.lock:
            self.throttle(peer)
            row = self.db.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
            if not row or not password_matches(old, row[0]):
                raise ServiceError(400, "当前密码不正确")
            encoded = password_hash(new)
            with self.db:
                self.db.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded, user_id))
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return self.new_session(user_id)

    def connections(self, user_id: str) -> list[dict]:
        with self.lock:
            return [{"id": row["id"], **json.loads(row["config"]), "key_mask": "••••••••", "has_key": True,
                     "verification": "not_verified"}
                    for row in self.db.execute("SELECT id,config FROM connections WHERE user_id=? ORDER BY rowid",
                                               (user_id,))]

    def save_connection(self, user_id: str, connection_id: str | None, config: dict,
                        api_key: str | None) -> dict:
        with self.lock, self.db:
            config = {name: config[name] for name in ("label", "provider", "model", "generation")}
            row = self.db.execute("SELECT * FROM connections WHERE id=? AND user_id=?",
                                  (connection_id, user_id)).fetchone() if connection_id else None
            if connection_id and row is None:
                raise ServiceError(404, "连接不存在")
            if row is None and not api_key:
                raise ServiceError(422, "新连接需要填写 API Key")
            if row is None and self.db.execute("SELECT count(*) FROM connections WHERE user_id=?",
                                              (user_id,)).fetchone()[0] >= 30:
                raise ServiceError(409, "最多保存 30 个模型连接")
            if row and api_key is None and json.loads(row["config"])["provider"] != config["provider"]:
                raise ServiceError(422, "更换供应商时请重新填写 API Key")
            encrypted = self.cipher.encrypt(canonical_json({"user_id": user_id, "api_key": api_key}).encode()) if (
                api_key is not None
            ) else row["encrypted_key"]
            connection_id = connection_id or uuid.uuid4().hex
            # All secrets are excluded from config. The origin is fixed, never a user URL.
            config = {**config, "base_url": PROVIDER_URLS[config["provider"]]}
            self.db.execute("INSERT INTO connections VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                            "config=excluded.config,encrypted_key=excluded.encrypted_key",
                            (connection_id, user_id, canonical_json(config), encrypted))
            return next(c for c in self.connections(user_id) if c["id"] == connection_id)

    def delete_connection(self, user_id: str, connection_id: str) -> None:
        with self.lock, self.db:
            if not self.db.execute("DELETE FROM connections WHERE id=? AND user_id=?",
                                   (connection_id, user_id)).rowcount:
                raise ServiceError(404, "连接不存在")

    def connection_snapshot(self, user_id: str, connection_id: str) -> tuple[dict, str]:
        """Safe effective config + opaque credential revision; does not decrypt the key."""
        with self.lock:
            row = self.db.execute("SELECT config,encrypted_key FROM connections WHERE id=? AND user_id=?",
                                  (connection_id, user_id)).fetchone()
            if row is None:
                raise ServiceError(404, "连接不存在")
            return json.loads(row["config"]), hashlib.sha256(row["encrypted_key"]).hexdigest()

    def table_plan(self, user_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT version,payload FROM table_plans WHERE user_id=?", (user_id,)).fetchone()
            return {"version": row["version"], "plan": json.loads(row["payload"])} if row else {
                "version": None, "plan": None
            }

    def save_table_plan(self, user_id: str, plan: dict, expected_version: str | None) -> dict:
        with self.lock, self.db:
            current = self.table_plan(user_id)
            for profile in plan["ai_profiles"]:
                self.connection_snapshot(user_id, profile["connection_id"])
            # Same payload retries are safe even when the first response was lost.
            if current["plan"] == plan:
                return current
            if current["version"] != expected_version:
                raise ServiceError(409, "参赛配置已在其他页面更新，请先重新读取；不会覆盖")
            version = uuid.uuid4().hex
            self.db.execute("INSERT INTO table_plans VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                            "version=excluded.version,payload=excluded.payload",
                            (user_id, version, canonical_json(plan)))
            return {"version": version, "plan": plan}

    def credential(self, user_id: str, connection_id: str) -> str:
        """Trusted adapter boundary only; never register an HTTP route for this."""
        with self.lock:
            row = self.db.execute("SELECT encrypted_key FROM connections WHERE id=? AND user_id=?",
                                  (connection_id, user_id)).fetchone()
            if row is None:
                raise ServiceError(404, "连接不存在")
            try:
                value = json.loads(self.cipher.decrypt(row[0]))
                if value["user_id"] != user_id:
                    raise InvalidToken
                return value["api_key"]
            except (InvalidToken, ValueError, KeyError) as exc:
                raise ServiceError(503, "密钥无法解密，请检查本机密钥文件") from exc

    def close(self):
        with self.lock:
            self.db.close()
