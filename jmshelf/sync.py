"""JM 账号收藏夹 → 本地收藏夹（单向镜像）。

- JM 里的每个收藏夹对应一个本地收藏夹“JM·<名字>”；不在任何收藏夹里的收藏归到“JM·未分类”。
- 镜像只改这些“JM·”收藏夹的内容，不碰本地自建的收藏夹，也不删除书库里的作品和 PDF。
- 被收藏的作品没入库的安排入库，入库后自动生成 PDF。
"""
from .db import JM_FOLDER_PREFIX

UNCATEGORIZED = ("0", "未分类")


def _jm_lists(data: dict) -> list[tuple[str, str, list[int]]]:
    lists = [(f["id"], f["name"], f["items"]) for f in data["folders"]]
    if data["uncategorized"]:
        lists.append((*UNCATEGORIZED, data["uncategorized"]))
    return lists


def preview(db, data: dict) -> dict:
    """不改数据库，统计同步后要做的事。"""
    ids = list(dict.fromkeys(i for _, _, items in _jm_lists(data) for i in items))
    return {
        "total": data["total"],
        "folders": [(JM_FOLDER_PREFIX + name, len(items)) for _, name, items in _jm_lists(data)],
        "archive": [i for i in ids if not db.fully_archived(i)],
        "pdf": [i for i in ids if db.fully_archived(i) and not db.pdf_record(i)],
    }


def apply(db, user_id: int, data: dict) -> dict:
    before = {r["jm_id"] for r in db.conn.execute(
        "SELECT fi.jm_id FROM folder_items fi JOIN folders f ON f.id=fi.folder_id "
        "WHERE f.user_id=? AND f.source='jm'", (user_id,))}
    keep = set()
    for jm_folder_id, name, items in _jm_lists(data):
        folder_id = db.jm_folder(user_id, jm_folder_id, name)
        db.mirror_folder(folder_id, items)
        keep.add(folder_id)
    db.drop_jm_folders_except(user_id, keep)
    plan = preview(db, data)
    after = {i for _, _, items in _jm_lists(data) for i in items}
    plan["changed"] = sorted(before | after)
    return plan


def summary_counts(plan: dict) -> str:
    return f"共 {plan['total']} 本，需要入库 {len(plan['archive'])} 本，需要生成 PDF {len(plan['pdf'])} 本"
