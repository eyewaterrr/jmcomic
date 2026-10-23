"""SQLite 索引：书目、章节、每页偏移、任务、收藏、用户和阅读进度。

所有访问都在同一个 asyncio 事件循环里进行，单个连接即可。
"""
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    jm_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '',
    works TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    chapter_count INTEGER NOT NULL,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
    photo_id INTEGER PRIMARY KEY,
    jm_id INTEGER NOT NULL,
    sort INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    page_count INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    file_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    added_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chapters_by_album ON chapters(jm_id, sort);
CREATE TABLE IF NOT EXISTS pages (
    photo_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    name TEXT NOT NULL,
    offset INTEGER NOT NULL,
    size INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    PRIMARY KEY (photo_id, idx)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    jm_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    step TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    chat_id INTEGER,
    msg_id INTEGER,
    run_after INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites (
    jm_id INTEGER PRIMARY KEY,
    msg_ids TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    read_mode TEXT NOT NULL DEFAULT 'scroll',
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS progress (
    user_id INTEGER NOT NULL,
    jm_id INTEGER NOT NULL,
    photo_id INTEGER NOT NULL,
    page INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, jm_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> int:
    return int(time.time())


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def _one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def _all(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    # ---- users ----
    def ensure_owner(self, owner_id: int):
        self.conn.execute(
            "INSERT INTO users(user_id, role, added_at) VALUES(?, 'owner', ?) "
            "ON CONFLICT(user_id) DO UPDATE SET role='owner'",
            (owner_id, _now()),
        )

    def user(self, user_id: int):
        return self._one("SELECT * FROM users WHERE user_id=?", user_id)

    def users(self):
        return self._all("SELECT * FROM users ORDER BY role DESC, added_at")

    def add_user(self, user_id: int, name: str = ""):
        self.conn.execute(
            "INSERT OR IGNORE INTO users(user_id, role, name, added_at) VALUES(?, 'user', ?, ?)",
            (user_id, name, _now()),
        )

    def remove_user(self, user_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM users WHERE user_id=? AND role!='owner'", (user_id,))
        return cur.rowcount > 0

    def set_user_name(self, user_id: int, name: str):
        self.conn.execute("UPDATE users SET name=? WHERE user_id=?", (name, user_id))

    def set_read_mode(self, user_id: int, mode: str):
        self.conn.execute("UPDATE users SET read_mode=? WHERE user_id=?", (mode, user_id))

    # ---- albums / chapters / pages ----
    def upsert_album(self, info: dict):
        self.conn.execute(
            "INSERT INTO albums(jm_id, title, authors, works, tags, chapter_count, added_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(jm_id) DO UPDATE SET title=excluded.title, "
            "authors=excluded.authors, works=excluded.works, tags=excluded.tags, "
            "chapter_count=excluded.chapter_count",
            (
                info["jm_id"], info["title"], json.dumps(info["authors"], ensure_ascii=False),
                json.dumps(info["works"], ensure_ascii=False), json.dumps(info["tags"], ensure_ascii=False),
                len(info["chapters"]), _now(),
            ),
        )

    def album(self, jm_id: int):
        return self._one("SELECT * FROM albums WHERE jm_id=?", jm_id)

    def albums(self):
        return self._all(
            "SELECT a.*, COUNT(c.photo_id) AS archived, COALESCE(SUM(c.page_count), 0) AS pages, "
            "MAX(c.added_at) AS last_added, (f.jm_id IS NOT NULL) AS fav "
            "FROM albums a LEFT JOIN chapters c ON c.jm_id=a.jm_id "
            "LEFT JOIN favorites f ON f.jm_id=a.jm_id GROUP BY a.jm_id ORDER BY last_added DESC"
        )

    def archived_ids(self, jm_ids: list[int]) -> set[int]:
        if not jm_ids:
            return set()
        marks = ",".join("?" * len(jm_ids))
        rows = self._all(f"SELECT DISTINCT jm_id FROM chapters WHERE jm_id IN ({marks})", *jm_ids)
        return {r["jm_id"] for r in rows}

    def chapters(self, jm_id: int):
        return self._all("SELECT * FROM chapters WHERE jm_id=? ORDER BY sort", jm_id)

    def chapter(self, photo_id: int):
        return self._one("SELECT * FROM chapters WHERE photo_id=?", photo_id)

    def add_chapter(self, jm_id: int, photo_id: int, sort: int, title: str, msg_id: int,
                    file_size: int, sha256: str, pages: list):
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute(
                "INSERT OR REPLACE INTO chapters(photo_id, jm_id, sort, title, page_count, msg_id, "
                "file_size, sha256, added_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (photo_id, jm_id, sort, title, len(pages), msg_id, file_size, sha256, _now()),
            )
            self.conn.execute("DELETE FROM pages WHERE photo_id=?", (photo_id,))
            self.conn.executemany(
                "INSERT INTO pages(photo_id, idx, name, offset, size, width, height) VALUES(?, ?, ?, ?, ?, ?, ?)",
                [(photo_id, i, p.name, p.offset, p.size, p.width, p.height) for i, p in enumerate(pages, 1)],
            )

    def pages(self, photo_id: int):
        return self._all("SELECT * FROM pages WHERE photo_id=? ORDER BY idx", photo_id)

    def page(self, photo_id: int, idx: int):
        return self._one("SELECT * FROM pages WHERE photo_id=? AND idx=?", photo_id, idx)

    def remove_album(self, jm_id: int) -> tuple[list[int], list[int]]:
        """删除索引，返回需要在频道里删除的书库消息 ID 和收藏消息 ID。"""
        lib = [r["msg_id"] for r in self.chapters(jm_id)]
        fav_row = self.favorite(jm_id)
        fav = [int(x) for x in fav_row["msg_ids"].split(",")] if fav_row else []
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute("DELETE FROM pages WHERE photo_id IN (SELECT photo_id FROM chapters WHERE jm_id=?)", (jm_id,))
            for table in ("chapters", "favorites", "progress", "albums"):
                self.conn.execute(f"DELETE FROM {table} WHERE jm_id=?", (jm_id,))
        return lib, fav

    # ---- favorites ----
    def favorite(self, jm_id: int):
        return self._one("SELECT * FROM favorites WHERE jm_id=?", jm_id)

    def add_favorite(self, jm_id: int, msg_ids: list[int], file_size: int, sha256: str, user_id: int):
        self.conn.execute(
            "INSERT OR REPLACE INTO favorites(jm_id, msg_ids, file_size, sha256, user_id, added_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (jm_id, ",".join(map(str, msg_ids)), file_size, sha256, user_id, _now()),
        )

    # ---- progress ----
    def progress(self, user_id: int, jm_id: int):
        return self._one("SELECT * FROM progress WHERE user_id=? AND jm_id=?", user_id, jm_id)

    def all_progress(self, user_id: int) -> dict:
        return {r["jm_id"]: r for r in self._all("SELECT * FROM progress WHERE user_id=?", user_id)}

    def save_progress(self, user_id: int, jm_id: int, photo_id: int, page: int):
        self.conn.execute(
            "INSERT INTO progress(user_id, jm_id, photo_id, page, updated_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, jm_id) DO UPDATE SET photo_id=excluded.photo_id, page=excluded.page, "
            "updated_at=excluded.updated_at",
            (user_id, jm_id, photo_id, page, _now()),
        )

    # ---- jobs ----
    def add_job(self, kind: str, jm_id: int, user_id: int, chat_id: int | None,
                msg_id: int | None = None) -> tuple[int, bool]:
        """返回 (任务 ID, 是否新建)。同一作品同类任务未完成时不重复建。"""
        row = self.active_job(kind, jm_id)
        if row:
            return row["id"], False
        cur = self.conn.execute(
            "INSERT INTO jobs(kind, jm_id, user_id, status, chat_id, msg_id, created_at, updated_at) "
            "VALUES(?, ?, ?, 'queued', ?, ?, ?, ?)",
            (kind, jm_id, user_id, chat_id, msg_id, _now(), _now()),
        )
        return cur.lastrowid, True

    def job(self, job_id: int):
        return self._one("SELECT * FROM jobs WHERE id=?", job_id)

    def active_job(self, kind: str, jm_id: int):
        return self._one(
            "SELECT * FROM jobs WHERE kind=? AND jm_id=? AND status IN ('queued', 'running')", kind, jm_id
        )

    def next_job(self):
        return self._one(
            "SELECT * FROM jobs WHERE status='queued' AND run_after<=? ORDER BY id LIMIT 1", _now()
        )

    def recent_jobs(self, limit: int = 10):
        return self._all("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", limit)

    def update_job(self, job_id: int, **fields):
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def requeue_running(self):
        self.conn.execute("UPDATE jobs SET status='queued', step='' WHERE status='running'")

    # ---- meta ----
    def get_meta(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM meta WHERE key=?", key)
        return row["value"] if row else default

    def set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def backup_to(self, dest: Path):
        target = sqlite3.connect(dest)
        try:
            self.conn.backup(target)
        finally:
            target.close()
