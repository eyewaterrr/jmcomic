import json

import pytest

from jmshelf import meta, sync
from jmshelf.db import DB, DEFAULT_FOLDER
from jmshelf.pack import Page

OWNER = 1


def album(jm_id, chapters=1):
    return {"jm_id": jm_id, "title": f"作品{jm_id}", "authors": ["A"], "works": ["W"], "tags": ["t"],
            "chapters": [{"photo_id": jm_id + i, "sort": i + 1, "title": f"第{i + 1}章"} for i in range(chapters)]}


def archive(db, jm_id, chapters=1):
    info = album(jm_id, chapters)
    db.upsert_album(info)
    for c in info["chapters"]:
        db.add_chapter(jm_id, c["photo_id"], c["sort"], c["title"], 100 + c["sort"], 2048, "ab" * 32,
                       [Page("0001.webp", 39, 1000, 800, 1200), Page("0002.webp", 1100, 900, 800, 1100)])


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "t.db")
    d.ensure_owner(OWNER)
    return d


def test_old_pdf_favorites_migrate_into_default_folder(tmp_path):
    d = DB(tmp_path / "old.db")
    archive(d, 10)
    d.add_pdf_record(10, [3], 999, "x", OWNER)
    d.ensure_owner(OWNER)
    assert [f["name"] for f in d.album_folders(OWNER, 10)] == [DEFAULT_FOLDER]
    d.ensure_owner(OWNER)  # 只迁移一次
    assert len(d.folders(OWNER)) == 1


def test_folder_lifecycle(db):
    archive(db, 10)
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
    assert db.is_favorited(OWNER, 10) and db.favorited_by_anyone(10)
    lib = {r["jm_id"]: r for r in db.albums(OWNER)}
    assert sorted(int(x) for x in lib[10]["folder_ids"].split(",")) == sorted([default, fid])

    db.rename_folder(OWNER, fid, "精选")
    db.delete_folder(OWNER, fid)
    assert [f["id"] for f in db.album_folders(OWNER, 10)] == [default]
    db.unfavorite(OWNER, 10)
    assert not db.is_favorited(OWNER, 10)
    assert not db.is_favorited(2, 10)


def test_jm_sync_mirrors_folders_and_plans_work(db):
    archive(db, 10)
    archive(db, 20)
    db.add_pdf_record(20, [5], 1, "x", OWNER)
    local = db.create_folder(OWNER, "自建")
    db.set_album_folders(OWNER, 10, {local})

    data = {"total": 3, "folders": [{"id": "7", "name": "同人", "items": [10, 30]}], "uncategorized": [20]}
    plan = sync.apply(db, OWNER, data)
    assert plan["archive"] == [30] and plan["pdf"] == [10]
    names = {f["name"]: f["count"] for f in db.folders(OWNER)}
    assert names["JM·同人"] == 1 and names["JM·未分类"] == 1 and names["自建"] == 1

    # JM 那边移除了 10、删掉了“未分类”：只影响 JM· 收藏夹
    sync.apply(db, OWNER, {"total": 1, "folders": [{"id": "7", "name": "同人", "items": [30]}], "uncategorized": []})
    assert [f["name"] for f in db.album_folders(OWNER, 10)] == ["自建"]
    assert not db.is_favorited(OWNER, 20)
    assert "JM·未分类" not in {f["name"] for f in db.folders(OWNER)}


def test_meta_export_and_restore_roundtrip(db, tmp_path):
    archive(db, 10, chapters=2)
    db.add_pdf_record(10, [7, 8], 4096, "s1,s2", OWNER)
    db.set_album_folders(OWNER, 10, {db.default_folder(OWNER), db.create_folder(OWNER, "精选")})
    sync.apply(db, OWNER, {"total": 1, "folders": [{"id": "9", "name": "收藏", "items": [10]}], "uncategorized": []})

    data_dir = tmp_path / "data"
    assert meta.export_all(db, data_dir) == 1
    saved = json.loads((meta.meta_dir(data_dir) / "JM10.json").read_text())
    assert saved["pdf"]["msg_ids"] == [7, 8] and len(saved["chapters"]) == 2
    assert saved["chapters"][0]["pages"][0] == ["0001.webp", 39, 1000, 800, 1200]

    fresh = DB(tmp_path / "fresh.db")
    fresh.ensure_owner(OWNER)
    assert meta.restore_all(fresh, meta.meta_dir(data_dir)) == 1
    assert fresh.album_meta(10) == db.album_meta(10)
