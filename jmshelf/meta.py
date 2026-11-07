"""每本作品一份元数据 JSON，保存在服务器上，便于迁移和恢复。

位置：<数据目录>/meta/JM<号>.json。内容包括作品信息、章节（序号、标题、页数）、精选记录、所在收藏夹，
一本 1–3KB。v1.1 的旧格式（带频道消息 ID 和每页偏移）也能恢复。

收藏夹变化、上传精选、补元数据时自动更新；也可以用命令整体导出或恢复：
    python -m jmshelf export-meta        从数据库重新生成全部 JSON
    python -m jmshelf restore-meta 目录   用 JSON 恢复数据库（已有记录会被覆盖）
"""
import json
import os
from pathlib import Path


def meta_dir(data_dir: Path) -> Path:
    return data_dir / "meta"


def write_album(db, data_dir: Path, jm_id: int):
    folder = meta_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"JM{jm_id}.json"
    meta = db.album_meta(jm_id)
    if meta is None:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
    os.replace(tmp, path)


def export_all(db, data_dir: Path) -> int:
    ids = [r["jm_id"] for r in db.conn.execute("SELECT jm_id FROM albums")]
    folder = meta_dir(data_dir)
    if folder.exists():  # 删掉数据库里已经没有的作品
        for path in folder.glob("JM*.json"):
            if path.stem[2:].isdigit() and int(path.stem[2:]) not in ids:
                path.unlink()
    for jm_id in ids:
        write_album(db, data_dir, jm_id)
    return len(ids)


def restore_all(db, folder: Path) -> int:
    count = 0
    for path in sorted(folder.glob("JM*.json")):
        db.restore_album_meta(json.loads(path.read_text()))
        count += 1
    return count
