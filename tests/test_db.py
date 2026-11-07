import json
import sqlite3
from pathlib import Path

import pytest

from jmshelf import meta, sync
from jmshelf.db import DB, DEFAULT_FOLDER

OWNER = 1
SCHEMA_V11 = (Path(__file__).parent / "schema_v11.sql").read_text()


def album(jm_id, chapters=1):
    return {"jm_id": jm_id, "title": f"作品{jm_id}", "authors": ["A"], "works": ["W"], "tags": ["t"],
            "chapters": [{"photo_id": jm_id + i, "sort": i + 1, "title": f"第{i + 1}章"} for i in range(chapters)]}


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "t.db")
    d.ensure_owner(OWNER)
    return d


# ---- v1.1 → v3 迁移 ----
def make_v11_db(path: Path):
    """按 v1.1 的表结构建一个库：3 本已入库（1 本读过、1 本收藏且有 PDF、1 本只入库），任务和频道备份记录。"""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_V11)
    conn.execute("ALTER TABLE jobs ADD COLUMN batch_id INTEGER")
    conn.execute("INSERT INTO users(user_id, role, added_at) VALUES(?, 'owner', 1)", (OWNER,))
    for jm_id in (10, 20, 30):
        conn.execute("INSERT INTO albums VALUES(?, ?, '[\"A\"]', '[]', '[]', 1, 1)", (jm_id, f"作品{jm_id}"))
        conn.execute("INSERT INTO chapters VALUES(?, ?, 1, '', 2, ?, 2048, 'ab', 1)", (jm_id, jm_id, 100 + jm_id))
        conn.execute("INSERT INTO pages VALUES(?, 1, '0001.webp', 39, 1000, 800, 1200)", (jm_id,))
        conn.execute("INSERT INTO pages VALUES(?, 2, '0002.webp', 1100, 900, 800, 1100)", (jm_id,))
    conn.execute("INSERT INTO progress VALUES(?, 10, 10, 2, 5)", (OWNER,))
    conn.execute("INSERT INTO folders(id, user_id, name, is_default, created_at) VALUES(1, ?, ?, 1, 1)",
                 (OWNER, DEFAULT_FOLDER))
    conn.execute("INSERT INTO folder_items VALUES(1, 20, 1)")
    conn.execute("INSERT INTO favorites VALUES(20, '7,8', 4096, 's1,s2', ?, 3)", (OWNER,))
    conn.execute("INSERT INTO jobs(kind, jm_id, user_id, status, created_at, updated_at) VALUES('archive', 30, 1, 'done', 1, 1)")
    conn.execute("INSERT INTO jobs(kind, jm_id, user_id, status, created_at, updated_at) VALUES('favorite', 20, 1, 'done', 1, 1)")
    conn.executemany("INSERT INTO meta VALUES(?, ?)", [("backup_msgs", "[7]"), ("backup_at", "1"), ("folders_v1", "1")])
    conn.commit()
    conn.close()


def test_migrate_v11_to_v3(tmp_path):
    path = tmp_path / "jmshelf.db"
    make_v11_db(path)
    d = DB(path)

    assert (tmp_path / "jmshelf-pre-v3.db").exists(), "迁移前要留一份完整备份"
    assert "msg_id" not in d._columns("chapters") and not d._has_table("pages") and not d._has_table("favorites")
    assert d.chapter(10)["page_count"] == 2
    assert d.chapter_dims(10) == [[800, 1200], [800, 1100]]
    assert d.pick(20)["msg_ids"] == "7,8"
    assert [(j["kind"], j["jm_id"]) for j in d.recent_jobs()] == [("publish", 20)]
    assert d.get_meta("backup_msgs") == "" and d.get_meta("schema") == "3"
    # 只入库、没收藏也没读过的 30 放进默认收藏夹；读过的 10 不动；本来就收藏的 20 不重复
    assert d.is_favorited(OWNER, 30) and not d.is_favorited(OWNER, 10) and d.is_favorited(OWNER, 20)
    assert {r["jm_id"] for r in d.shelf(OWNER)} == {10, 20, 30}

    DB(path)  # 再打开一次不会重复迁移
    assert len(d.folders(OWNER)) == 1


# ---- v3 ----
def test_shelf_is_favorites_plus_read(db):
    for jm_id in (10, 20, 30):
        db.upsert_album(album(jm_id))
    db.add_to_folder(db.default_folder(OWNER), 10)
    db.save_progress(OWNER, 20, 20, 3)
    db.add_pick(20, [5], 1, "x", OWNER)
    shelf = {r["jm_id"]: r for r in db.shelf(OWNER)}
    assert set(shelf) == {10, 20}                    # 30 只有元数据，不在书架上
    assert shelf[20]["p_page"] == 3 and shelf[20]["pick"] and not shelf[20]["folder_ids"]
    assert shelf[10]["folder_ids"] and not shelf[10]["p_photo"]
    assert db.shelf(2) == []                          # 别的用户看不到


def test_album_stub_and_chapter_upsert_keeps_page_info(db):
    db.ensure_album_stub(10)
    assert db.stub_albums() == [10]
    db.upsert_album(album(10, chapters=2))
    assert db.stub_albums() == []
    db.set_chapter_pages(10, 40)
    db.set_chapter_dims(10, [[800, 1200], None])
    db.upsert_album(dict(album(10, chapters=1), title="改名了"))   # JM 那边删掉了第 2 章
    assert db.album(10)["title"] == "改名了"
    assert [c["photo_id"] for c in db.chapters(10)] == [10]
    assert db.chapter(10)["page_count"] == 40 and db.chapter_dims(10) == [[800, 1200], None]


def test_folder_lifecycle(db):
    db.upsert_album(album(10))
    default = db.default_folder(OWNER)
    fid = db.create_folder(OWNER, "  好看  的 ")
    assert db.folder(OWNER, fid)["name"] == "好看 的"
    with pytest.raises(ValueError):
        db.create_folder(OWNER, "好看 的")
    with pytest.raises(ValueError):
        db.create_folder(OWNER, "JM·假的")
    with pytest.raises(ValueError):
        db.delete_folder(OWNER, default)

    db.set_album_folders(OWNER, 10, {default, fid})
    assert db.is_favorited(OWNER, 10)
    shelf = {r["jm_id"]: r for r in db.shelf(OWNER)}
    assert sorted(int(x) for x in shelf[10]["folder_ids"].split(",")) == sorted([default, fid])

    db.rename_folder(OWNER, fid, "精选")
    db.delete_folder(OWNER, fid)
    assert [f["id"] for f in db.album_folders(OWNER, 10)] == [default]
    db.unfavorite(OWNER, 10)
    assert not db.is_favorited(OWNER, 10)
    assert not db.is_favorited(2, 10)


def test_jm_sync_only_mirrors_folders(db):
    db.upsert_album(album(10))
    db.upsert_album(album(20))
    local = db.create_folder(OWNER, "自建")
    db.set_album_folders(OWNER, 10, {local})

    data = {"total": 3, "folders": [{"id": "7", "name": "同人", "items": [10, 30]}], "uncategorized": [20]}
    plan = sync.apply(db, OWNER, data)
    assert plan["new"] == [30] and plan["changed"] == [10, 20, 30]
    names = {f["name"]: f["count"] for f in db.folders(OWNER)}
    assert names["JM·同人"] == 2 and names["JM·未分类"] == 1 and names["自建"] == 1
    assert db.recent_jobs() == []                     # v3：同步不建任何任务

    # JM 那边移除了 10、删掉了“未分类”：只影响 JM· 收藏夹
    sync.apply(db, OWNER, {"total": 1, "folders": [{"id": "7", "name": "同人", "items": [30]}], "uncategorized": []})
    assert [f["name"] for f in db.album_folders(OWNER, 10)] == ["自建"]
    assert not db.is_favorited(OWNER, 20)
    assert "JM·未分类" not in {f["name"] for f in db.folders(OWNER)}


def test_meta_export_and_restore_roundtrip(db, tmp_path):
    db.upsert_album(album(10, chapters=2))
    db.set_chapter_pages(10, 33)
    db.add_pick(10, [7, 8], 4096, "s1,s2", OWNER)
    db.set_album_folders(OWNER, 10, {db.default_folder(OWNER), db.create_folder(OWNER, "精选")})
    sync.apply(db, OWNER, {"total": 1, "folders": [{"id": "9", "name": "收藏", "items": [10]}], "uncategorized": []})

    data_dir = tmp_path / "data"
    assert meta.export_all(db, data_dir) == 1
    saved = json.loads((meta.meta_dir(data_dir) / "JM10.json").read_text())
    assert saved["version"] == 2 and saved["pick"]["msg_ids"] == [7, 8]
    assert saved["chapters"][0] == {"photo_id": 10, "sort": 1, "title": "第1章", "page_count": 33}

    fresh = DB(tmp_path / "fresh.db")
    fresh.ensure_owner(OWNER)
    assert meta.restore_all(fresh, meta.meta_dir(data_dir)) == 1
    assert fresh.album_meta(10) == db.album_meta(10)


def test_restore_accepts_v11_meta_files(db):
    old = {"version": 1, "jm_id": 10, "title": "旧", "authors": [], "works": [], "tags": [], "chapter_count": 1,
           "added_at": 1, "chapters": [{"photo_id": 11, "sort": 1, "title": "", "msg_id": 5, "file_size": 1,
                                         "sha256": "x", "added_at": 1,
                                         "pages": [["0001.webp", 39, 10, 800, 1200]]}],
           "pdf": {"msg_ids": [3], "file_size": 9, "sha256": ["s"], "user_id": OWNER, "added_at": 2},
           "folders": {str(OWNER): [{"name": DEFAULT_FOLDER, "source": "local", "jm_folder_id": "", "default": True}]}}
    db.restore_album_meta(old)
    assert db.chapter(11)["page_count"] == 1 and db.chapter_dims(11) == [[800, 1200]]
    assert db.pick(10)["msg_ids"] == "3" and db.is_favorited(OWNER, 10)


def test_album_states_for_search_results(db):
    db.add_pick(10, [5], 1, "x", OWNER)
    db.add_to_folder(db.default_folder(OWNER), 10)
    db.add_job("publish", 30, OWNER, None)
    states = db.album_states(OWNER, [10, 30, 40])
    assert states[10] == {"fav": True, "pick": True, "picking": False}
    assert states[30]["picking"] and not states[40]["picking"]
    assert db.album_states(2, [10])[10]["fav"] is False


def test_remove_pick_returns_message_ids(db):
    db.add_pick(10, [7, 8], 1, "x", OWNER)
    assert db.remove_pick(10) == [7, 8]
    assert db.pick(10) is None and db.remove_pick(10) == []
