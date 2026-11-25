"""Small, transactional account store for a single standalone service process."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

SYNC_KEYS = frozenset({
    "paperlens.prefs", "paperlens.terms", "paperlens:reading-type",
    "paperlens:reading-language", "paperlens:term-replacements",
    "paperlens:citation-copy-format", "paperlens:reference-source-preference",
})
SYNC_PREFIXES = (
    "paperlens:reading-position:", "paperlens:annotation-draft:",
    "paperlens:reading-note-followup:", "paperlens.draft:",
)
MAX_SYNC_BYTES = 10 * 1024**2
MAX_SYNC_KEYS = 2048


def sync_key(key: str) -> bool:
    return (isinstance(key, str) and len(key) <= 512
            and (key in SYNC_KEYS or key.startswith(SYNC_PREFIXES)))


def email_address(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", value):
        raise HTTPException(422, "请输入有效的邮箱地址。")
    return value


class AccountStore:
    def __init__(self, path: Path, secret: bytes, *, max_users: int = 100):
        self.path, self.secret, self.max_users = path, secret, max_users
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS identities (
                    provider TEXT NOT NULL, subject TEXT NOT NULL, user_id TEXT NOT NULL,
                    PRIMARY KEY(provider, subject), FOREIGN KEY(user_id) REFERENCES users(id));
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id));
                CREATE TABLE IF NOT EXISTS challenges (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL, code TEXT NOT NULL,
                    browser TEXT NOT NULL, user_id TEXT, expires REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS oauth (
                    state TEXT PRIMARY KEY, browser TEXT NOT NULL, verifier TEXT NOT NULL,
                    user_id TEXT, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS throttle (
                    bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS browser_data (
                    user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
                    version INTEGER NOT NULL, size INTEGER NOT NULL,
                    PRIMARY KEY(user_id, key), FOREIGN KEY(user_id) REFERENCES users(id));
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY, user_id TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def digest(self, value: str) -> str:
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def limit(self, bucket: str, count: int, seconds: int):
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM throttle WHERE expires < ?", (now,))
            row = db.execute("SELECT count FROM throttle WHERE bucket=?", (bucket,)).fetchone()
            if row and row[0] >= count:
                raise HTTPException(429, "操作过于频繁，请稍后再试。", headers={"Retry-After": str(seconds)})
            db.execute("""INSERT INTO throttle VALUES (?,1,?) ON CONFLICT(bucket)
                          DO UPDATE SET count=count+1""", (bucket, now + seconds))

    def _identity(self, db, provider: str, subject: str, name: str, user_id: str | None):
        found = db.execute("SELECT user_id FROM identities WHERE provider=? AND subject=?", (provider, subject)).fetchone()
        if found:
            if user_id and found[0] != user_id:
                raise HTTPException(409, "此登录方式已绑定其他账户。请先登录该账户，不会自动合并资料。")
            return found[0]
        if not user_id:
            if db.execute("SELECT count(*) FROM users").fetchone()[0] >= self.max_users:
                raise HTTPException(403, "当前注册名额已满，请联系管理员。")
            user_id = uuid.uuid4().hex
            db.execute("INSERT INTO users VALUES (?,?,?)", (user_id, name[:80], time.time()))
        db.execute("INSERT INTO identities VALUES (?,?,?)", (provider, subject, user_id))
        return user_id

    def identity(self, provider: str, subject: str, name: str, user_id: str | None = None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._identity(db, provider, subject, name, user_id)

    def challenge(self, email: str, browser: str, user_id: str | None = None):
        ident, code = secrets.token_urlsafe(24), f"{secrets.randbelow(1000000):06d}"
        with self.db() as db:
            db.execute("DELETE FROM challenges WHERE expires < ? OR (email=? AND browser=?)", (time.time(), email, self.digest(browser)))
            db.execute("INSERT INTO challenges VALUES (?,?,?,?,?,?,0)",
                       (ident, email, self.digest(ident + code), self.digest(browser), user_id, time.time() + 600))
        return ident, code

    def verify(self, ident: str, code: str, browser: str, user_id: str | None = None):
        result = None
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM challenges WHERE id=?", (ident,)).fetchone()
            if (row and row["expires"] > time.time() and row["attempts"] < 5
                    and hmac.compare_digest(row["browser"], self.digest(browser))
                    and row["user_id"] == user_id):
                db.execute("UPDATE challenges SET attempts=attempts+1 WHERE id=?", (ident,))
                if hmac.compare_digest(row["code"], self.digest(ident + code)):
                    result = self._identity(db, "email", row["email"], row["email"].split("@")[0], user_id)
                    db.execute("DELETE FROM challenges WHERE id=?", (ident,))
        if result is None:
            raise HTTPException(400, "验证码无效或已过期，请重新获取。")
        return result

    def session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (self.digest(token), user_id, time.time() + 7 * 86400))
        return token

    def user(self, token: str | None):
        if not token or len(token) > 128:
            return None
        with self.db() as db:
            row = db.execute("""SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id
                              WHERE token=? AND expires>?""", (self.digest(token), time.time())).fetchone()
            return dict(row) if row else None

    def identities(self, user_id: str):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT provider,subject FROM identities WHERE user_id=?", (user_id,))]

    def logout(self, token: str):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (self.digest(token),))

    def oauth_start(self, browser: str, verifier: str, user_id: str | None = None):
        state = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("DELETE FROM oauth WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO oauth VALUES (?,?,?,?,?)", (self.digest(state), self.digest(browser), verifier, user_id, time.time() + 600))
        return state

    def oauth_finish(self, state: str, browser: str, user_id: str | None = None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM oauth WHERE state=?", (self.digest(state),)).fetchone()
            if not row or row["expires"] < time.time() or row["user_id"] != user_id or not hmac.compare_digest(row["browser"], self.digest(browser)):
                raise HTTPException(400, "登录验证已失效，请重新开始 GitHub 登录。")
            db.execute("DELETE FROM oauth WHERE state=?", (self.digest(state),))
            return dict(row)

    def snapshot(self, user_id: str, policy):
        with self.db() as db:
            return {r["key"]: {"value": policy.unseal(r["value"]) if r["value"] is not None else None, "version": r["version"]}
                    for r in db.execute("SELECT * FROM browser_data WHERE user_id=?", (user_id,))}

    def sync(self, user_id: str, changes: list, policy):
        if not isinstance(changes, list) or len(changes) > 100:
            raise HTTPException(422, "每次最多同步 100 项资料。")
        seen = set()
        for item in changes:
            if (not isinstance(item, dict) or not sync_key(item.get("key"))
                    or item["key"] in seen or type(item.get("version")) is not int
                    or item["version"] < 0 or (item.get("value") is not None and
                    (not isinstance(item["value"], str) or len(item["value"].encode()) > 1024**2))):
                raise HTTPException(422, "同步资料格式或大小不符合要求。")
            seen.add(item["key"])
        accepted, conflicts = {}, {}
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in changes:
                key, value, version = item["key"], item.get("value"), item["version"]
                row = db.execute("SELECT * FROM browser_data WHERE user_id=? AND key=?", (user_id, key)).fetchone()
                old_version = row["version"] if row else 0
                old_value = policy.unseal(row["value"]) if row and row["value"] is not None else None
                if old_version != version and old_value != value:
                    conflicts[key] = {"value": old_value, "version": old_version}
                    continue
                if row and old_value == value:
                    accepted[key] = {"value": value, "version": old_version}
                    continue
                version = old_version + 1
                db.execute("""INSERT INTO browser_data VALUES (?,?,?,?,?) ON CONFLICT(user_id,key)
                    DO UPDATE SET value=excluded.value,version=excluded.version,size=excluded.size""",
                           (user_id, key, policy.seal(value) if value is not None else None, version,
                            len(value.encode()) if value else 0))
                accepted[key] = {"value": value, "version": version}
            size, count = db.execute("SELECT coalesce(sum(size),0),count(*) FROM browser_data WHERE user_id=?", (user_id,)).fetchone()
            if size > MAX_SYNC_BYTES or count > MAX_SYNC_KEYS:
                raise HTTPException(413, "账户同步资料已达上限，请先导出并清理不需要的草稿。")
        return {"accepted": accepted, "conflicts": conflicts}

    def export_browser(self, user_id: str, policy) -> str:
        return json.dumps({key: item["value"] for key, item in self.snapshot(user_id, policy).items()
                           if item["value"] is not None}, ensure_ascii=False)
