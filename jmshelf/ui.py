"""Bot 和任务共用的消息文本与按钮。"""
import json

from telethon import Button, types

from .fmt import clip, esc

KIND_NAME = {"archive": "入库", "favorite": "收藏 PDF"}


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


def album_view(cfg, db, storage, album: dict, user_id: int) -> tuple[str, list]:
    """作品详情。album 是 jm.album() 的结果；文字控制在图片说明的 1024 字以内。"""
    jm_id = album["jm_id"]
    chapters = album["chapters"]
    archived = {r["photo_id"]: r for r in db.chapters(jm_id)}
    done = sum(1 for c in chapters if c["photo_id"] in archived)
    folders = db.album_folders(user_id, jm_id)
    pdf = db.pdf_record(jm_id)
    archive_job = db.active_job("archive", jm_id)
    pdf_job = db.active_job("favorite", jm_id)

    if archive_job:
        state = "入库中"
    elif done == 0:
        state = "未入库"
    elif done < len(chapters):
        state = f"部分入库（{done}/{len(chapters)} 章）"
    else:
        state = "已入库"

    lines = [
        f"<b>{esc(clip(album['title'], 200))}</b>",
        f"JM{jm_id}",
        f"作者：{esc(_names(album['authors'], 80))}",
        f"作品：{esc(_names(album['works'], 80))}",
        f"标签：{esc(_names(album['tags'], 200))}",
        f"章节：{len(chapters)}    页数：{album.get('page_count') or '-'}",
        f"状态：{state}",
        "收藏：" + (esc(clip("已收藏（" + "、".join(f["name"] for f in folders) + "）", 120)) if folders else "未收藏"),
    ]
    if folders or pdf or pdf_job:
        lines.append("PDF：" + ("已生成" if pdf else "生成中" if pdf_job else "等入库完成后生成"))

    rows = []
    first = []
    if done < len(chapters) and not archive_job:
        first.append(Button.inline("入库", f"ar:{jm_id}"))
    if done:
        read = webapp_button(cfg, "在线阅读", f"?read={jm_id}")
        if read:
            first.append(read)
    first.append(Button.inline("取消收藏", f"uf:{jm_id}") if folders else Button.inline("收藏", f"fv:{jm_id}"))
    rows.append(first)
    links = []
    if archived:
        top = min(archived.values(), key=lambda r: r["sort"])
        links.append(Button.url("书库帖子", storage.post_link(storage.channel_id, top["msg_id"])))
    if pdf:
        links.append(Button.url("PDF 帖子", storage.post_link(storage.fav_channel_id, int(pdf["msg_ids"].split(",")[0]))))
    links.append(Button.inline("刷新", f"r:{jm_id}"))
    rows.append(links)
    return "\n".join(lines), rows
