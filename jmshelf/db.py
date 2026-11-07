"""SQLite：作品元数据、章节信息、收藏夹、精选记录、任务、用户和阅读进度。

所有访问都在同一个 asyncio 事件循环里进行，单个连接即可。

概念（v3）：
- 漫画内容不存在这里，也不存在 Telegram 书库频道：阅读时从 JM 取，服务器只做缓存（cache.py）。
- “收藏” = 作品在某个用户的任意一个收藏夹里（folders / folder_items），不触发任何任务。
- “精选” = 单独上传到精选频道的 PDF，记录在 picks 表（全局一份）。
- albums / chapters 只存收藏、读过或上传过精选的作品的元数据，书架和阅读进度靠它们显示，不用每次问 JM。
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
    page_count INTEGER NOT NULL DEFAULT 0,
    dims TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chapters_by_album ON chapters(jm_id, sort);
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
    updated_at INTEGER NOT NULL,
    batch_id INTEGER
);
CREATE TABLE IF NOT EXISTS picks (
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
SCHEMA_VERSION = "3"


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

    def _one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def _all(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _has_table(self, table: str) -> bool:
        return self._one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", table) is not None

    # ---- 迁移 ----
    def _migrate(self):
        if "batch_id" not in self._columns("jobs"):
            self.conn.execute("ALTER TABLE jobs ADD COLUMN batch_id INTEGER")
        if "msg_id" in self._columns("chapters"):
            self._migrate_v3()
        if not self.get_meta("schema"):
            self.set_meta("schema", SCHEMA_VERSION)

    def _migrate_v3(self):
        """v1.1 → v3：取消入库。迁移前先在同目录留一份完整备份（<库名>-pre-v3.db）。

        - chapters 只留章节信息，每页尺寸从 pages 表折进 dims（阅读页靠它排版）；删掉 pages 表。
        - PDF 记录表 favorites 改名 picks；入库任务删掉，favorite 任务改名 publish。
        - 只入库、既没收藏也没读过的作品，v3 的书架上看不到，放进所有者的默认收藏夹。
        - 频道备份的记录删掉（v3 不再往频道发数据库备份）。
        """
        backup = self.path.with_name(self.path.stem + "-pre-v3.db")
        if not backup.exists():
            self.backup_to(backup)
        dims: dict[int, list] = {}
        if self._has_table("pages"):
            for r in self._all("SELECT photo_id, width, height FROM pages ORDER BY photo_id, idx"):
                dims.setdefault(r["photo_id"], []).append([r["width"], r["height"]])
        owner = self._one("SELECT user_id FROM users WHERE role='owner' ORDER BY added_at LIMIT 1")
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute(
                "CREATE TABLE chapters_v3 (photo_id INTEGER PRIMARY KEY, jm_id INTEGER NOT NULL, "
                "sort INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', page_count INTEGER NOT NULL DEFAULT 0, "
                "dims TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL)")
            self.conn.execute(
                "INSERT INTO chapters_v3(photo_id, jm_id, sort, title, page_count, dims, updated_at) "
                "SELECT photo_id, jm_id, sort, title, page_count, '', added_at FROM chapters")
            for photo_id, sizes in dims.items():
                self.conn.execute("UPDATE chapters_v3 SET dims=? WHERE photo_id=?",
                                  (json.dumps(sizes, separators=(",", ":")), photo_id))
            self.conn.execute("DROP TABLE chapters")
            self.conn.execute("ALTER TABLE chapters_v3 RENAME TO chapters")
            self.conn.execute("CREATE INDEX IF NOT EXISTS chapters_by_album ON chapters(jm_id, sort)")
            self.conn.execute("DROP TABLE IF EXISTS pages")
            if self._has_table("favorites"):
                self.conn.execute("INSERT OR REPLACE INTO picks(jm_id, msg_ids, file_size, sha256, user_id, added_at) "
                                  "SELECT jm_id, msg_ids, file_size, sha256, user_id, added_at FROM favorites")
                self.conn.execute("DROP TABLE favorites")
            self.conn.execute("DELETE FROM jobs WHERE kind='archive'")
            self.conn.execute("UPDATE jobs SET kind='publish' WHERE kind='favorite'")
            if owner:
                lonely = self._all(
                    "SELECT jm_id FROM albums a WHERE NOT EXISTS (SELECT 1 FROM folder_items fi WHERE fi.jm_id=a.jm_id) "
                    "AND NOT EXISTS (SELECT 1 FROM progress p WHERE p.jm_id=a.jm_id)")
                folder = self.default_folder(owner["user_id"])
                for r in lonely:
                    self.add_to_folder(folder, r["jm_id"])
            self.conn.execute("DELETE FROM meta WHERE key IN ('backup_msgs', 'backup_at')")
            self.set_meta("schema", SCHEMA_VERSION)
            self.set_meta("meta_version", "")  # 元数据 JSON 格式变了，启动时重新导出

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
            for row in self._all("SELECT jm_id FROM picks"):
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

    # ---- 作品和章节（元数据） ----
    def upsert_album(self, info: dict, added_at: int | None = None):
        """写入或更新作品元数据；info 是 jm.album() 的结果，带 chapters 时一并更新章节信息。"""
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
        if info.get("chapters"):
            self.upsert_chapters(info["jm_id"], info["chapters"])

    def ensure_album_stub(self, jm_id: int):
        """先占个位置（标题为空），让书架马上显示这本；元数据之后由后台补上。"""
        self.conn.execute(
            "INSERT OR IGNORE INTO albums(jm_id, title, authors, works, tags, chapter_count, added_at) "
            "VALUES(?, '', '[]', '[]', '[]', 0, ?)", (jm_id, _now()))

    def album(self, jm_id: int):
        return self._one("SELECT * FROM albums WHERE jm_id=?", jm_id)

    def stub_albums(self) -> list[int]:
        return [r["jm_id"] for r in self._all("SELECT jm_id FROM albums WHERE title='' ORDER BY added_at")]

    def shelf(self, user_id: int):
        """书架：该用户收藏的作品 + 读过的作品，最近有动静（阅读或收藏）的排前面。"""
        return self._all(
            "SELECT a.*, p.photo_id AS p_photo, p.page AS p_page, p.updated_at AS p_at, "
            "(SELECT GROUP_CONCAT(fi.folder_id) FROM folder_items fi JOIN folders fo ON fo.id=fi.folder_id "
            " WHERE fi.jm_id=a.jm_id AND fo.user_id=?) AS folder_ids, "
            "(SELECT MAX(fi.added_at) FROM folder_items fi JOIN folders fo ON fo.id=fi.folder_id "
            " WHERE fi.jm_id=a.jm_id AND fo.user_id=?) AS fav_at, "
            "EXISTS(SELECT 1 FROM picks k WHERE k.jm_id=a.jm_id) AS pick, "
            "(SELECT COALESCE(SUM(page_count), 0) FROM chapters c WHERE c.jm_id=a.jm_id) AS pages "
            "FROM albums a LEFT JOIN progress p ON p.jm_id=a.jm_id AND p.user_id=? "
            "WHERE p.jm_id IS NOT NULL OR EXISTS(SELECT 1 FROM folder_items fi JOIN folders fo ON fo.id=fi.folder_id "
            " WHERE fi.jm_id=a.jm_id AND fo.user_id=?) "
            "ORDER BY MAX(COALESCE(p.updated_at, 0), COALESCE(fav_at, 0)) DESC, a.jm_id DESC",
            user_id, user_id, user_id, user_id,
        )

    def album_states(self, user_id: int, jm_ids: list[int]) -> dict[int, dict]:
        """一批作品的状态：是否被该用户收藏、是否已上传精选、是否正在上传。"""
        out = {i: {"fav": False, "pick": False, "picking": False} for i in jm_ids}
        if not jm_ids:
            return out
        marks = ",".join("?" * len(jm_ids))
        for r in self._all(f"SELECT DISTINCT fi.jm_id FROM folder_items fi JOIN folders f ON f.id=fi.folder_id "
                           f"WHERE f.user_id=? AND fi.jm_id IN ({marks})", user_id, *jm_ids):
            out[r["jm_id"]]["fav"] = True
        for r in self._all(f"SELECT jm_id FROM picks WHERE jm_id IN ({marks})", *jm_ids):
            out[r["jm_id"]]["pick"] = True
        for r in self._all(f"SELECT jm_id FROM jobs WHERE kind='publish' AND status IN ('queued', 'running') "
                           f"AND jm_id IN ({marks})", *jm_ids):
            out[r["jm_id"]]["picking"] = True
        return out

    def upsert_chapters(self, jm_id: int, chapters: list[dict]):
        """更新章节序号和标题；已知的页数和每页尺寸保留。JM 那边去掉的章节一并删掉。"""
        with self.conn:
            self.conn.execute("BEGIN")
            for c in chapters:
                self.conn.execute(
                    "INSERT INTO chapters(photo_id, jm_id, sort, title, updated_at) VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(photo_id) DO UPDATE SET jm_id=excluded.jm_id, sort=excluded.sort, "
                    "title=excluded.title, updated_at=excluded.updated_at",
                    (c["photo_id"], jm_id, c["sort"], c["title"], _now()))
            keep = [c["photo_id"] for c in chapters]
            marks = ",".join("?" * len(keep))
            self.conn.execute(f"DELETE FROM chapters WHERE jm_id=? AND photo_id NOT IN ({marks})", (jm_id, *keep))

    def chapters(self, jm_id: int):
        return self._all("SELECT * FROM chapters WHERE jm_id=? ORDER BY sort", jm_id)

    def chapter(self, photo_id: int):
        return self._one("SELECT * FROM chapters WHERE photo_id=?", photo_id)

    def set_chapter_pages(self, photo_id: int, page_count: int):
        self.conn.execute("UPDATE chapters SET page_count=? WHERE photo_id=?", (page_count, photo_id))

    def chapter_dims(self, photo_id: int) -> list:
        row = self.chapter(photo_id)
        try:
            return json.loads(row["dims"]) if row and row["dims"] else []
        except ValueError:
            return []

    def set_chapter_dims(self, photo_id: int, dims: list):
        self.conn.execute("UPDATE chapters SET dims=? WHERE photo_id=?",
                          (json.dumps(dims, separators=(",", ":")), photo_id))

    # ---- 精选 ----
    def pick(self, jm_id: int):
        return self._one("SELECT * FROM picks WHERE jm_id=?", jm_id)

    def add_pick(self, jm_id: int, msg_ids: list[int], file_size: int, sha256: str, user_id: int,
                 added_at: int | None = None):
        self.conn.execute(
            "INSERT OR REPLACE INTO picks(jm_id, msg_ids, file_size, sha256, user_id, added_at) VALUES(?, ?, ?, ?, ?, ?)",
            (jm_id, ",".join(map(str, msg_ids)), file_size, sha256, user_id, added_at or _now()),
        )

    def remove_pick(self, jm_id: int) -> list[int]:
        """删除精选记录，返回要在精选频道里删除的消息 ID。"""
        row = self.pick(jm_id)
        if row is None:
            return []
        self.conn.execute("DELETE FROM picks WHERE jm_id=?", (jm_id,))
        return [int(x) for x in row["msg_ids"].split(",")]

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
            "SELECT f.*, (SELECT COUNT(*) FROM folder_items fi WHERE fi.folder_id=f.id) AS count "
            "FROM folders f WHERE f.user_id=? ORDER BY f.is_default DESC, f.source, f.created_at, f.id",
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

    def jobs_ahead(self, job_id: int) -> int:
        """排在这个任务前面、还没做完的任务数。"""
        return self._one("SELECT COUNT(*) AS n FROM jobs WHERE id<? AND status IN ('queued', 'running')", job_id)["n"]

    def job_counts(self) -> dict:
        rows = self._all("SELECT status, COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running') GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def queued_count(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running')")["n"]

    def update_job(self, job_id: int, **fields):
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def requeue_running(self):
        self.conn.execute("UPDATE jobs SET status='queued', step='' WHERE status='running'")

    # ---- 导出 / 恢复（每本一份 JSON，见 meta.py） ----
    def album_meta(self, jm_id: int) -> dict | None:
        album = self.album(jm_id)
        if album is None:
            return None
        pick = self.pick(jm_id)
        folders = {}
        for r in self._all(
                "SELECT f.user_id, f.name, f.source, f.jm_folder_id, f.is_default FROM folders f "
                "JOIN folder_items fi ON fi.folder_id=f.id WHERE fi.jm_id=?", jm_id):
            folders.setdefault(str(r["user_id"]), []).append(
                {"name": r["name"], "source": r["source"], "jm_folder_id": r["jm_folder_id"],
                 "default": bool(r["is_default"])})
        return {
            "version": 2,
            "jm_id": jm_id,
            "title": album["title"],
            "authors": json.loads(album["authors"] or "[]"),
            "works": json.loads(album["works"] or "[]"),
            "tags": json.loads(album["tags"] or "[]"),
            "chapter_count": album["chapter_count"],
            "added_at": album["added_at"],
            "chapters": [{"photo_id": c["photo_id"], "sort": c["sort"], "title": c["title"],
                          "page_count": c["page_count"]} for c in self.chapters(jm_id)],
            "pick": {
                "msg_ids": [int(x) for x in pick["msg_ids"].split(",")], "file_size": pick["file_size"],
                "sha256": pick["sha256"].split(","), "user_id": pick["user_id"], "added_at": pick["added_at"],
            } if pick else None,
            "folders": folders,
        }

    def restore_album_meta(self, meta: dict):
        """恢复一本的元数据；也接受 v1.1 的旧格式（章节里的频道消息和每页偏移忽略，每页尺寸保留）。"""
        info = dict(meta, chapters=[{"photo_id": c["photo_id"], "sort": c["sort"], "title": c["title"]}
                                    for c in meta["chapters"]])
        self.upsert_album(info, added_at=meta["added_at"])
        for c in meta["chapters"]:
            pages = c.get("page_count") or len(c.get("pages", []))
            self.set_chapter_pages(c["photo_id"], pages)
            if c.get("pages"):
                self.set_chapter_dims(c["photo_id"], [[p[3], p[4]] for p in c["pages"]])
        pick = meta.get("pick") or meta.get("pdf")
        if pick:
            self.add_pick(meta["jm_id"], pick["msg_ids"], pick["file_size"], ",".join(pick["sha256"]),
                          pick["user_id"], added_at=pick["added_at"])
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
