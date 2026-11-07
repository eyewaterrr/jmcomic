"""Bot 和任务共用的消息文本与按钮。"""
import json

from telethon import Button, types

from .fmt import clip, esc

KIND_NAME = {"publish": "上传精选"}


def webapp_button(cfg, text: str, query: str = ""):
    """私聊里打开 Mini App 的按钮；没配置网址时返回 None。"""
    if not cfg.public_url:
        return None
    return types.KeyboardInlineButton(text, types.InlineButtonTypeWebView(f"{cfg.public_url}/{query}"))


def startapp_link(bot_username: str, param: str) -> str:
    """频道帖子用的直达链接，需要在 BotFather 里设置 Main Mini App。"""
    return f"https://t.me/{bot_username}?startapp={param}"


def _names(value, width: int) -> str:
    items = json.loads(value) if isinstance(value, str) else value
    return clip("、".join(items), width) if items else "-"


def pick_link(storage, pick) -> str:
    return storage.post_link(storage.fav_channel_id, int(pick["msg_ids"].split(",")[0]))


def album_view(cfg, db, storage, album: dict, user_id: int) -> tuple[str, list]:
    """作品详情。album 是 jm.album() 的结果；文字控制在图片说明的 1024 字以内。"""
    jm_id = album["jm_id"]
    chapters = album["chapters"]
    folders = db.album_folders(user_id, jm_id)
    pick = db.pick(jm_id)
    picking = db.active_job("publish", jm_id)
    progress = db.progress(user_id, jm_id)

    lines = [
        f"<b>{esc(clip(album['title'], 200))}</b>",
        f"JM{jm_id}",
        f"作者：{esc(_names(album['authors'], 80))}",
        f"作品：{esc(_names(album['works'], 80))}",
        f"标签：{esc(_names(album['tags'], 200))}",
        f"章节：{len(chapters)}    页数：{album.get('page_count') or '-'}",
        "收藏：" + (esc(clip("已收藏（" + "、".join(f["name"] for f in folders) + "）", 120)) if folders else "未收藏"),
        "精选：" + ("已上传" if pick else "上传中" if picking else "未上传"),
    ]
    if progress:
        at = next((c for c in chapters if c["photo_id"] == progress["photo_id"]), None)
        if at:
            lines.append(f"读到：第 {at['sort']} 章 第 {progress['page']} 页" if len(chapters) > 1
                         else f"读到：第 {progress['page']} 页")

    rows = []
    first = []
    read = webapp_button(cfg, "继续阅读" if progress else "在线阅读", f"?read={jm_id}")
    if read:
        first.append(read)
    first.append(Button.inline("取消收藏", f"uf:{jm_id}") if folders else Button.inline("收藏", f"fv:{jm_id}"))
    rows.append(first)
    if pick:
        rows.append([Button.url("查看精选", pick_link(storage, pick))])
    elif not picking:
        rows.append([Button.inline("上传精选（生成 PDF 发到精选频道）", f"pb:{jm_id}")])
    authors = [Button.inline(f"作者：{clip(name, 16)}", f"au:{jm_id}:{i}") for i, name in enumerate(album["authors"][:2])]
    if authors:
        rows.append(authors)
    rows.append([Button.inline("刷新", f"r:{jm_id}")])
    return "\n".join(lines), rows
