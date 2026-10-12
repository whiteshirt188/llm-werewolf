"""SQLite transactions for secret state, filtered stream, receipts and attempts."""

import json
import sqlite3
from pathlib import Path

from .profiles import canonical_json


class Store:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, self.SCHEMA_VERSION}:
            self.db.close()
            raise ValueError("不支持的数据库版本")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS games (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, snapshot TEXT NOT NULL,
                visible TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_game ON games ((1))
                WHERE status IN ('running','paused');
            CREATE TABLE IF NOT EXISTS stream (
                game_id TEXT NOT NULL REFERENCES games(id), seq INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(game_id, seq)
            );
            CREATE TABLE IF NOT EXISTS receipts (
                game_id TEXT NOT NULL REFERENCES games(id), operation_id TEXT NOT NULL,
                request_hash TEXT NOT NULL, response TEXT NOT NULL,
                PRIMARY KEY(game_id, operation_id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, game_id TEXT NOT NULL REFERENCES games(id),
                status TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS game_starts (
                user_id TEXT NOT NULL, operation_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                game_id TEXT NOT NULL REFERENCES games(id), encrypted_token TEXT NOT NULL,
                PRIMARY KEY(user_id,operation_id)
            );
            PRAGMA user_version=1;
        """)

    def load_games(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT snapshot FROM games")]

    def commit(self, game_id: str, snapshot: dict, visible: dict,
               receipt: tuple[str, str, dict] | None = None,
               attempt: tuple[str, dict] | None = None,
               start: tuple[str, str, str, str] | None = None,
               new_attempt: dict | None = None) -> bool:
        """All mutations commit together; callers publish memory only after success."""
        encoded = canonical_json(visible)
        with self.db:
            old = self.db.execute("SELECT visible FROM games WHERE id=?", (game_id,)).fetchone()
            self.db.execute(
                "INSERT INTO games VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status,snapshot=excluded.snapshot,visible=excluded.visible",
                (game_id, snapshot["status"], canonical_json(snapshot), encoded),
            )
            changed = old is None or old[0] != encoded
            if changed:
                seq = self.cursor(game_id) + 1
                self.db.execute("INSERT INTO stream VALUES(?,?,?)", (game_id, seq, encoded))
            if receipt:
                operation_id, request_hash, response = receipt
                self.db.execute("INSERT INTO receipts VALUES(?,?,?,?)",
                                (game_id, operation_id, request_hash, canonical_json(response)))
            if attempt:
                attempt_id, payload = attempt
                count = self.db.execute("UPDATE attempts SET status=?,payload=? WHERE id=? AND game_id=?",
                                        (payload["status"], canonical_json(payload), attempt_id, game_id)).rowcount
                if count != 1:
                    raise ValueError("调用记录不存在，事务回滚")
            if new_attempt:
                self.db.execute("INSERT INTO attempts VALUES(?,?,?,?)",
                                (new_attempt["attempt_id"], game_id, "in_flight", canonical_json(new_attempt)))
            if start:
                user_id, operation_id, request_hash, encrypted_token = start
                self.db.execute("INSERT INTO game_starts VALUES(?,?,?,?,?)",
                                (user_id, operation_id, request_hash, game_id, encrypted_token))
        return changed

    def game_start(self, user_id: str, operation_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM game_starts WHERE user_id=? AND operation_id=?",
                              (user_id, operation_id)).fetchone()
        return dict(row) if row else None

    def start_for_game(self, user_id: str, game_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM game_starts WHERE user_id=? AND game_id=?",
                              (user_id, game_id)).fetchone()
        return dict(row) if row else None

    def latest_start(self, user_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM game_starts WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (user_id,)).fetchone()
        return dict(row) if row else None

    def receipt(self, game_id: str, operation_id: str) -> tuple[str, dict] | None:
        row = self.db.execute("SELECT request_hash,response FROM receipts WHERE game_id=? AND operation_id=?",
                              (game_id, operation_id)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def start_attempt(self, attempt_id: str, game_id: str, payload: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO attempts VALUES(?,?,?,?)",
                            (attempt_id, game_id, "in_flight", canonical_json(payload)))

    def interrupt_attempts(self) -> None:
        with self.db:
            rows = self.db.execute("SELECT id,payload FROM attempts WHERE status='in_flight'").fetchall()
            for row in rows:
                payload = json.loads(row[1])
                payload["status"] = "interrupted"
                self.db.execute("UPDATE attempts SET status='interrupted',payload=? WHERE id=?",
                                (canonical_json(payload), row[0]))

    def attempts(self, game_id: str) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM attempts WHERE game_id=? ORDER BY rowid", (game_id,)
        )]

    def cursor(self, game_id: str) -> int:
        return self.db.execute("SELECT COALESCE(MAX(seq),0) FROM stream WHERE game_id=?", (game_id,)).fetchone()[0]

    def events(self, game_id: str, after: int) -> list[dict]:
        return [{"id": row[0], "state": json.loads(row[1])} for row in self.db.execute(
            "SELECT seq,payload FROM stream WHERE game_id=? AND seq>? ORDER BY seq", (game_id, after)
        )]

    def close(self) -> None:
        self.db.close()
