"""SQLite 索引：书目、章节、每页偏移、任务、收藏夹、PDF 记录、用户和阅读进度。

所有访问都在同一个 asyncio 事件循环里进行，单个连接即可。

概念：
- “收藏” = 作品在某个用户的任意一个收藏夹里（folders / folder_items）。
- 被收藏的作品会生成 PDF 发到精选频道，记录在 favorites 表（PDF 记录，全局一份）。
- 取消收藏只移出收藏夹，不删除 PDF。
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
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'local',
    jm_folder_id TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE (user_id, name)
);
CREATE TABLE IF NOT EXISTS folder_items (
    folder_id INTEGER NOT NULL,
    jm_id INTEGER NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (folder_id, jm_id)
);
CREATE INDEX IF NOT EXISTS folder_items_by_album ON folder_items(jm_id);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    chat_id INTEGER,
    msg_id INTEGER,
    finished INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
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

DEFAULT_FOLDER = "默认收藏夹"
JM_FOLDER_PREFIX = "JM·"
FOLDER_NAME_MAX = 30


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
        self._migrate()

    def _migrate(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "batch_id" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN batch_id INTEGER")

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
        # 第一版的“收藏”只有 PDF 记录，迁移进所有者的默认收藏夹
        if not self.get_meta("folders_v1"):
            folder = self.default_folder(owner_id)
            for row in self._all("SELECT jm_id FROM favorites"):
                self.add_to_folder(folder, row["jm_id"])
            self.set_meta("folders_v1", "1")

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
    def upsert_album(self, info: dict, added_at: int | None = None):
        self.conn.execute(
            "INSERT INTO albums(jm_id, title, authors, works, tags, chapter_count, added_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(jm_id) DO UPDATE SET title=excluded.title, "
            "authors=excluded.authors, works=excluded.works, tags=excluded.tags, "
            "chapter_count=excluded.chapter_count",
            (
                info["jm_id"], info["title"], json.dumps(info["authors"], ensure_ascii=False),
                json.dumps(info["works"], ensure_ascii=False), json.dumps(info["tags"], ensure_ascii=False),
                info.get("chapter_count", len(info.get("chapters", []))), added_at or _now(),
            ),
        )

    def album(self, jm_id: int):
        return self._one("SELECT * FROM albums WHERE jm_id=?", jm_id)

    def albums(self, user_id: int):
        return self._all(
            "SELECT a.*, "
            "(SELECT COUNT(*) FROM chapters c WHERE c.jm_id=a.jm_id) AS archived, "
            "(SELECT COALESCE(SUM(page_count), 0) FROM chapters c WHERE c.jm_id=a.jm_id) AS pages, "
            "(SELECT MAX(added_at) FROM chapters c WHERE c.jm_id=a.jm_id) AS last_added, "
            "EXISTS(SELECT 1 FROM favorites f WHERE f.jm_id=a.jm_id) AS pdf, "
            "(SELECT GROUP_CONCAT(fi.folder_id) FROM folder_items fi JOIN folders fo ON fo.id=fi.folder_id "
            " WHERE fi.jm_id=a.jm_id AND fo.user_id=?) AS folder_ids "
            "FROM albums a ORDER BY last_added DESC",
            user_id,
        )

    def archived_ids(self, jm_ids: list[int]) -> set[int]:
        if not jm_ids:
            return set()
        marks = ",".join("?" * len(jm_ids))
        rows = self._all(f"SELECT DISTINCT jm_id FROM chapters WHERE jm_id IN ({marks})", *jm_ids)
        return {r["jm_id"] for r in rows}

    def fully_archived(self, jm_id: int) -> bool:
        album = self.album(jm_id)
        return bool(album) and len(self.chapters(jm_id)) >= album["chapter_count"]

    def chapters(self, jm_id: int):
        return self._all("SELECT * FROM chapters WHERE jm_id=? ORDER BY sort", jm_id)

    def chapter(self, photo_id: int):
        return self._one("SELECT * FROM chapters WHERE photo_id=?", photo_id)

    def add_chapter(self, jm_id: int, photo_id: int, sort: int, title: str, msg_id: int,
                    file_size: int, sha256: str, pages: list, added_at: int | None = None):
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute(
                "INSERT OR REPLACE INTO chapters(photo_id, jm_id, sort, title, page_count, msg_id, "
                "file_size, sha256, added_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (photo_id, jm_id, sort, title, len(pages), msg_id, file_size, sha256, added_at or _now()),
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
        """删除索引，返回需要在频道里删除的书库消息 ID 和 PDF 消息 ID。"""
        lib = [r["msg_id"] for r in self.chapters(jm_id)]
        pdf = self.pdf_record(jm_id)
        pdf_ids = [int(x) for x in pdf["msg_ids"].split(",")] if pdf else []
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute("DELETE FROM pages WHERE photo_id IN (SELECT photo_id FROM chapters WHERE jm_id=?)", (jm_id,))
            for table in ("chapters", "favorites", "progress", "folder_items", "albums"):
                self.conn.execute(f"DELETE FROM {table} WHERE jm_id=?", (jm_id,))
        return lib, pdf_ids

    # ---- PDF 记录（表名沿用 favorites） ----
    def pdf_record(self, jm_id: int):
        return self._one("SELECT * FROM favorites WHERE jm_id=?", jm_id)

    def add_pdf_record(self, jm_id: int, msg_ids: list[int], file_size: int, sha256: str, user_id: int,
                       added_at: int | None = None):
        self.conn.execute(
            "INSERT OR REPLACE INTO favorites(jm_id, msg_ids, file_size, sha256, user_id, added_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (jm_id, ",".join(map(str, msg_ids)), file_size, sha256, user_id, added_at or _now()),
        )

    # ---- 收藏夹 ----
    def default_folder(self, user_id: int) -> int:
        row = self._one("SELECT id FROM folders WHERE user_id=? AND is_default=1", user_id)
        if row:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO folders(user_id, name, is_default, created_at) VALUES(?, ?, 1, ?)",
            (user_id, DEFAULT_FOLDER, _now()),
        )
        return cur.lastrowid

    def folders(self, user_id: int):
        self.default_folder(user_id)
        return self._all(
            "SELECT f.*, (SELECT COUNT(*) FROM folder_items fi JOIN albums a ON a.jm_id=fi.jm_id "
            "WHERE fi.folder_id=f.id) AS count FROM folders f WHERE f.user_id=? "
            "ORDER BY f.is_default DESC, f.source, f.created_at, f.id",
            user_id,
        )

    def folder(self, user_id: int, folder_id: int):
        return self._one("SELECT * FROM folders WHERE id=? AND user_id=?", folder_id, user_id)

    @staticmethod
    def check_folder_name(name: str) -> str:
        name = " ".join(str(name).split())
        if not name:
            raise ValueError("收藏夹名字不能为空")
        if len(name) > FOLDER_NAME_MAX:
            raise ValueError(f"收藏夹名字最多 {FOLDER_NAME_MAX} 个字")
        if name.startswith(JM_FOLDER_PREFIX):
            raise ValueError(f"“{JM_FOLDER_PREFIX}”开头的名字留给 JM 同步使用")
        return name

    def create_folder(self, user_id: int, name: str) -> int:
        name = self.check_folder_name(name)
        try:
            cur = self.conn.execute(
                "INSERT INTO folders(user_id, name, created_at) VALUES(?, ?, ?)", (user_id, name, _now()))
        except sqlite3.IntegrityError:
            raise ValueError("已经有同名的收藏夹")
        return cur.lastrowid

    def rename_folder(self, user_id: int, folder_id: int, name: str):
        row = self.folder(user_id, folder_id)
        if row is None or row["source"] != "local":
            raise ValueError("这个收藏夹不能改名")
        try:
            self.conn.execute("UPDATE folders SET name=? WHERE id=?", (self.check_folder_name(name), folder_id))
        except sqlite3.IntegrityError:
            raise ValueError("已经有同名的收藏夹")

    def delete_folder(self, user_id: int, folder_id: int):
        row = self.folder(user_id, folder_id)
        if row is None or row["is_default"]:
            raise ValueError("默认收藏夹不能删除")
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute("DELETE FROM folder_items WHERE folder_id=?", (folder_id,))
            self.conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))

    def add_to_folder(self, folder_id: int, jm_id: int):
        self.conn.execute(
            "INSERT OR IGNORE INTO folder_items(folder_id, jm_id, added_at) VALUES(?, ?, ?)",
            (folder_id, jm_id, _now()),
        )

    def album_folders(self, user_id: int, jm_id: int):
        return self._all(
            "SELECT f.* FROM folders f JOIN folder_items fi ON fi.folder_id=f.id "
            "WHERE f.user_id=? AND fi.jm_id=? ORDER BY f.is_default DESC, f.source, f.id",
            user_id, jm_id,
        )

    def set_album_folders(self, user_id: int, jm_id: int, folder_ids: set[int]):
        """设置作品在用户本地收藏夹里的归属；JM 同步来的收藏夹不受影响。"""
        local = {r["id"] for r in self._all("SELECT id FROM folders WHERE user_id=? AND source='local'", user_id)}
        if not folder_ids <= local:
            raise ValueError("收藏夹不存在或不能手动修改")
        with self.conn:
            self.conn.execute("BEGIN")
            for fid in local - folder_ids:
                self.conn.execute("DELETE FROM folder_items WHERE folder_id=? AND jm_id=?", (fid, jm_id))
            for fid in folder_ids:
                self.conn.execute(
                    "INSERT OR IGNORE INTO folder_items(folder_id, jm_id, added_at) VALUES(?, ?, ?)",
                    (fid, jm_id, _now()))

    def unfavorite(self, user_id: int, jm_id: int):
        self.conn.execute(
            "DELETE FROM folder_items WHERE jm_id=? AND folder_id IN (SELECT id FROM folders WHERE user_id=?)",
            (jm_id, user_id),
        )

    def is_favorited(self, user_id: int, jm_id: int) -> bool:
        return bool(self.album_folders(user_id, jm_id))

    def favorited_by_anyone(self, jm_id: int) -> bool:
        return self._one("SELECT 1 FROM folder_items WHERE jm_id=? LIMIT 1", jm_id) is not None

    # ---- JM 收藏夹同步 ----
    def jm_folder(self, user_id: int, jm_folder_id: str, name: str) -> int:
        full = JM_FOLDER_PREFIX + name
        row = self._one("SELECT id FROM folders WHERE user_id=? AND source='jm' AND jm_folder_id=?",
                        user_id, jm_folder_id)
        if row:
            self.conn.execute("UPDATE OR IGNORE folders SET name=? WHERE id=?", (full, row["id"]))
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO folders(user_id, name, source, jm_folder_id, created_at) VALUES(?, ?, 'jm', ?, ?)",
            (user_id, full, jm_folder_id, _now()),
        )
        return cur.lastrowid

    def mirror_folder(self, folder_id: int, jm_ids: list[int]):
        with self.conn:
            self.conn.execute("BEGIN")
            keep = set(jm_ids)
            current = {r["jm_id"] for r in self._all("SELECT jm_id FROM folder_items WHERE folder_id=?", folder_id)}
            for jm_id in current - keep:
                self.conn.execute("DELETE FROM folder_items WHERE folder_id=? AND jm_id=?", (folder_id, jm_id))
            for jm_id in keep - current:
                self.conn.execute("INSERT INTO folder_items(folder_id, jm_id, added_at) VALUES(?, ?, ?)",
                                  (folder_id, jm_id, _now()))

    def drop_jm_folders_except(self, user_id: int, keep: set[int]):
        for row in self._all("SELECT id FROM folders WHERE user_id=? AND source='jm'", user_id):
            if row["id"] not in keep:
                self.conn.execute("DELETE FROM folder_items WHERE folder_id=?", (row["id"],))
                self.conn.execute("DELETE FROM folders WHERE id=?", (row["id"],))

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

    # ---- jobs / batches ----
    def add_job(self, kind: str, jm_id: int, user_id: int, chat_id: int | None,
                msg_id: int | None = None, batch_id: int | None = None) -> tuple[int, bool]:
        """返回 (任务 ID, 是否新建)。同一作品同类任务未完成时不重复建。"""
        row = self.active_job(kind, jm_id)
        if row:
            return row["id"], False
        cur = self.conn.execute(
            "INSERT INTO jobs(kind, jm_id, user_id, status, chat_id, msg_id, batch_id, created_at, updated_at) "
            "VALUES(?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
            (kind, jm_id, user_id, chat_id, msg_id, batch_id, _now(), _now()),
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

    def queued_count(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running')")["n"]

    def update_job(self, job_id: int, **fields):
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def requeue_running(self):
        self.conn.execute("UPDATE jobs SET status='queued', step='' WHERE status='running'")

    def create_batch(self, user_id: int, label: str, chat_id: int | None, msg_id: int | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO batches(user_id, label, chat_id, msg_id, created_at) VALUES(?, ?, ?, ?, ?)",
            (user_id, label, chat_id, msg_id, _now()),
        )
        return cur.lastrowid

    def batch(self, batch_id: int):
        return self._one("SELECT * FROM batches WHERE id=?", batch_id)

    def batch_counts(self, batch_id: int) -> dict:
        rows = self._all("SELECT kind, status, COUNT(*) AS n FROM jobs WHERE batch_id=? GROUP BY kind, status", batch_id)
        return {(r["kind"], r["status"]): r["n"] for r in rows}

    def set_batch_msg(self, batch_id: int, msg_id: int):
        self.conn.execute("UPDATE batches SET msg_id=? WHERE id=?", (msg_id, batch_id))

    def finish_batch(self, batch_id: int):
        self.conn.execute("UPDATE batches SET finished=1 WHERE id=?", (batch_id,))

    # ---- 导出 / 恢复（每本一份 JSON，见 meta.py） ----
    def album_meta(self, jm_id: int) -> dict | None:
        album = self.album(jm_id)
        if album is None:
            return None
        pdf = self.pdf_record(jm_id)
        folders = {}
        for r in self._all(
                "SELECT f.user_id, f.name, f.source, f.jm_folder_id, f.is_default FROM folders f "
                "JOIN folder_items fi ON fi.folder_id=f.id WHERE fi.jm_id=?", jm_id):
            folders.setdefault(str(r["user_id"]), []).append(
                {"name": r["name"], "source": r["source"], "jm_folder_id": r["jm_folder_id"],
                 "default": bool(r["is_default"])})
        return {
            "version": 1,
            "jm_id": jm_id,
            "title": album["title"],
            "authors": json.loads(album["authors"]),
            "works": json.loads(album["works"]),
            "tags": json.loads(album["tags"]),
            "chapter_count": album["chapter_count"],
            "added_at": album["added_at"],
            "chapters": [
                {
                    "photo_id": c["photo_id"], "sort": c["sort"], "title": c["title"],
                    "msg_id": c["msg_id"], "file_size": c["file_size"], "sha256": c["sha256"],
                    "added_at": c["added_at"],
                    "pages": [[p["name"], p["offset"], p["size"], p["width"], p["height"]]
                              for p in self.pages(c["photo_id"])],
                }
                for c in self.chapters(jm_id)
            ],
            "pdf": {
                "msg_ids": [int(x) for x in pdf["msg_ids"].split(",")], "file_size": pdf["file_size"],
                "sha256": pdf["sha256"].split(","), "user_id": pdf["user_id"], "added_at": pdf["added_at"],
            } if pdf else None,
            "folders": folders,
        }

    def restore_album_meta(self, meta: dict):
        from .pack import Page

        self.upsert_album(meta, added_at=meta["added_at"])
        for c in meta["chapters"]:
            pages = [Page(*p) for p in c["pages"]]
            self.add_chapter(meta["jm_id"], c["photo_id"], c["sort"], c["title"], c["msg_id"],
                             c["file_size"], c["sha256"], pages, added_at=c["added_at"])
        if meta.get("pdf"):
            p = meta["pdf"]
            self.add_pdf_record(meta["jm_id"], p["msg_ids"], p["file_size"], ",".join(p["sha256"]),
                                p["user_id"], added_at=p["added_at"])
        for user_id, items in (meta.get("folders") or {}).items():
            uid = int(user_id)
            for f in items:
                if f["default"]:
                    folder_id = self.default_folder(uid)
                elif f["source"] == "jm":
                    folder_id = self.jm_folder(uid, f["jm_folder_id"], f["name"].removeprefix(JM_FOLDER_PREFIX))
                else:
                    row = self._one("SELECT id FROM folders WHERE user_id=? AND name=?", uid, f["name"])
                    folder_id = row["id"] if row else self.create_folder(uid, f["name"])
                self.add_to_folder(folder_id, meta["jm_id"])

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
