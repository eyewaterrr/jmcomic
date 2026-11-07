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
