"""Bot 私聊：搜索、作品详情、入库、收藏、任务列表、用户管理。只响应白名单用户。"""
import logging
import re
import time

from telethon import Button, events, functions, types

from . import ui
from .fmt import clip, esc, text_table

log = logging.getLogger(__name__)

JM_ID = re.compile(r"^(?:jm)?\s*(\d{2,9})$", re.IGNORECASE)
PER_VIEW = 10

HELP = """<b>JMComic 书库</b>

直接发送：
JM 号（如 1485198 或 JM1485198）：查看详情、入库、收藏
其他文字：按关键词搜索

命令：
/library  打开书架
/jobs  查看任务
/help  使用说明

所有者命令：
/users  查看用户
/adduser 用户ID  添加用户
/deluser 用户ID  删除用户
/remove JM号  从书库删除一本（同时删除频道帖子）"""


class Bot:
    def __init__(self, client, cfg, db, jm, worker, storage):
        self.client = client
        self.cfg = cfg
        self.db = db
        self.jm = jm
        self.worker = worker
        self.storage = storage
        self._searches: dict[int, dict] = {}

    def register(self):
        self.client.add_event_handler(self.on_message, events.NewMessage(incoming=True, func=lambda e: e.is_private))
        self.client.add_event_handler(self.on_callback, events.CallbackQuery())

    async def setup_menu(self):
        commands = [
            types.BotCommand("library", "打开书架"),
            types.BotCommand("jobs", "查看任务"),
            types.BotCommand("help", "使用说明"),
        ]
        await self.client(functions.bots.SetBotCommandsRequest(types.BotCommandScopeDefault(), "", commands))
        if self.cfg.public_url:
            await self.client(functions.bots.SetBotMenuButtonRequest(
                types.InputUserEmpty(), types.BotMenuButton("书架", self.cfg.public_url + "/")))

    def _role(self, user_id: int) -> str | None:
        row = self.db.user(user_id)
        return row["role"] if row else None

    # ---- 消息 ----
    async def on_message(self, event):
        uid = event.sender_id
        role = self._role(uid)
        if role is None:
            log.info("忽略非白名单用户 %s", uid)
            return
        row = self.db.user(uid)
        if not row["name"]:
            sender = await event.get_sender()
            self.db.set_user_name(uid, " ".join(filter(None, [sender.first_name, sender.last_name])))
        text = (event.raw_text or "").strip()
        if not text:
            return
        try:
            if text.startswith("/"):
                cmd, _, arg = text[1:].partition(" ")
                await self._command(event, uid, role, cmd.split("@")[0].lower(), arg.strip())
            elif m := JM_ID.match(text):
                await self._send_album(event, int(m.group(1)))
            else:
                await self._search(event, uid, text, 1, edit=False)
        except Exception as e:
            log.exception("处理消息失败")
            await event.respond(f"出错了：{esc(clip(str(e) or type(e).__name__, 200))}", parse_mode="html")

    async def _command(self, event, uid, role, cmd, arg):
        if cmd in ("start", "help"):
            button = ui.webapp_button(self.cfg, "打开书架")
            await event.respond(HELP, parse_mode="html", buttons=[[button]] if button else None)
        elif cmd == "library":
            button = ui.webapp_button(self.cfg, "打开书架")
            if button:
                await event.respond("点下面的按钮打开书架。", buttons=[[button]])
            else:
                await event.respond("阅读页还没有配置网址。")
        elif cmd == "search":
            if arg:
                await self._search(event, uid, arg, 1, edit=False)
            else:
                await event.respond("用法：/search 关键词")
        elif cmd == "jobs":
            text, buttons = self._jobs_view()
            await event.respond(text, parse_mode="html", buttons=buttons)
        elif cmd in ("users", "adduser", "deluser", "remove") and role != "owner":
            await event.respond("这个命令只有所有者能用。")
        elif cmd == "users":
            await event.respond(self._users_view(), parse_mode="html")
        elif cmd == "adduser":
            if not arg.isdigit():
                await event.respond("用法：/adduser 用户ID（纯数字）")
                return
            self.db.add_user(int(arg))
            await event.respond(f"已添加用户 {arg}。对方需要先给 Bot 发一条消息，才能收到 Bot 的通知。")
        elif cmd == "deluser":
            ok = arg.isdigit() and self.db.remove_user(int(arg))
            await event.respond(f"已删除用户 {arg}。" if ok else "没有找到这个用户（所有者不能删除）。")
        elif cmd == "remove":
            m = JM_ID.match(arg)
            if not m or not self.db.album(int(m.group(1))):
                await event.respond("用法：/remove JM号（只能删除已入库的作品）")
                return
            jm_id = int(m.group(1))
            album = self.db.album(jm_id)
            await event.respond(
                f"确认从书库删除 JM{jm_id}？\n{esc(clip(album['title'], 120))}\n\n"
                "书库频道和精选频道里的帖子会一起删除，无法恢复。",
                parse_mode="html",
                buttons=[[Button.inline("确认删除", f"rmy:{jm_id}"), Button.inline("取消", "x:0")]])
        else:
            await event.respond("不认识这个命令，发 /help 查看用法。")

    # ---- 搜索 ----
    async def _search(self, event, uid, query, view, edit):
        state = self._searches.get(uid)
        if not state or state["query"] != query:
            state = {"query": query, "pages": {}, "total": 0, "page_size": 0, "api_pages": 1}
            self._searches[uid] = state

        start = (view - 1) * PER_VIEW
        items = await self._search_items(state, start, PER_VIEW)
        if not items:
            text = f"搜索：{esc(query)}\n没有找到结果。"
            await (event.edit(text, parse_mode="html") if edit else event.respond(text, parse_mode="html"))
            return
        total = state["total"]
        views = max(1, -(-total // PER_VIEW))
        archived = self.db.archived_ids([it["jm_id"] for it in items])
        lines = [f"搜索：<b>{esc(query)}</b>", f"共 {total} 条，第 {view}/{views} 页", ""]
        for i, it in enumerate(items, 1):
            mark = "  [已入库]" if it["jm_id"] in archived else ""
            lines.append(f"{i}. JM{it['jm_id']}  {esc(clip(it['title'], 60))}{mark}")
        numbers = [Button.inline(str(i), f"a:{it['jm_id']}") for i, it in enumerate(items, 1)]
        rows = [numbers[:5], numbers[5:]] if len(numbers) > 5 else [numbers]
        nav = []
        if view > 1:
            nav.append(Button.inline("上一页", f"sp:{view - 1}"))
        if view < views:
            nav.append(Button.inline("下一页", f"sp:{view + 1}"))
        if nav:
            rows.append(nav)
        text = "\n".join(lines)
        if edit:
            await event.edit(text, parse_mode="html", buttons=rows)
        else:
            await event.respond(text, parse_mode="html", buttons=rows)

    async def _search_items(self, state, start, count):
        """JM 接口一页条数较多，这里按 10 条一屏切分，并缓存已取过的接口页。"""
        if not state["pages"]:
            first = await self.jm.search(state["query"], 1)
            state["pages"][1] = first["items"]
            state["total"] = first["total"]
            state["page_size"] = len(first["items"]) or 1
        size = state["page_size"]
        result = []
        index = start
        while len(result) < count and index < state["total"]:
            api_page = index // size + 1
            if api_page not in state["pages"]:
                state["pages"][api_page] = (await self.jm.search(state["query"], api_page))["items"]
            items = state["pages"][api_page]
            offset = index - (api_page - 1) * size
            if offset >= len(items):
                break
            result.append(items[offset])
            index += 1
        return result

    # ---- 作品 ----
    async def _album_message(self, jm_id: int):
        try:
            album = await self.jm.album(jm_id)
        except Exception as e:
            log.info("取 JM%s 详情失败：%s", jm_id, e)
            return f"找不到 JM{jm_id}，或者暂时取不到详情。", None
        return ui.album_view(self.cfg, self.db, self.storage, album)

    async def _send_album(self, event, jm_id: int):
        text, buttons = await self._album_message(jm_id)
        await event.respond(text, parse_mode="html", buttons=buttons, link_preview=False)

    # ---- 任务 / 用户 ----
    def _jobs_view(self):
        jobs = self.db.recent_jobs(10)
        if not jobs:
            return "还没有任务。", None
        rows = [[j["id"], j["jm_id"], j["kind"], j["status"]] for j in jobs]
        lines = ["<b>最近的任务</b>", text_table(["ID", "JM", "TYPE", "STATUS"], rows, right={0, 1})]
        for j in jobs:
            if j["status"] == "running" and j["step"]:
                lines.append(f"任务 {j['id']} 进行中：{esc(j['step'])}")
            elif j["status"] == "failed" and j["error"]:
                lines.append(f"任务 {j['id']} 失败：{esc(clip(j['error'], 120))}")
        return "\n".join(lines), [[Button.inline("刷新", "jobs:0")]]

    def _users_view(self):
        rows = [[u["user_id"], u["role"], time.strftime("%Y-%m-%d", time.localtime(u["added_at"]))]
                for u in self.db.users()]
        names = [f"{u['user_id']}：{esc(u['name'] or '（未知）')}" for u in self.db.users()]
        return "<b>用户</b>\n" + text_table(["USER_ID", "ROLE", "ADDED"], rows, right={0}) + "\n" + "\n".join(names)

    # ---- 按钮 ----
    async def on_callback(self, event):
        uid = event.sender_id
        role = self._role(uid)
        if role is None:
            await event.answer()
            return
        kind, _, arg = event.data.decode().partition(":")
        try:
            if kind == "a":
                await event.answer()
                await self._send_album(event, int(arg))
            elif kind == "r":
                text, buttons = await self._album_message(int(arg))
                await event.edit(text, parse_mode="html", buttons=buttons, link_preview=False)
                await event.answer("已刷新")
            elif kind == "sp":
                state = self._searches.get(uid)
                if not state:
                    await event.answer("搜索已过期，请重新发送关键词", alert=True)
                    return
                await event.answer()
                await self._search(event, uid, state["query"], int(arg), edit=True)
            elif kind in ("ar", "fv"):
                job_kind = "archive" if kind == "ar" else "favorite"
                status, _ = await self.worker.submit(job_kind, int(arg), uid)
                await event.answer("已加入队列" if status == "created" else "已经在队列里了")
                text, buttons = await self._album_message(int(arg))
                await event.edit(text, parse_mode="html", buttons=buttons, link_preview=False)
            elif kind == "jobs":
                text, buttons = self._jobs_view()
                await event.edit(text, parse_mode="html", buttons=buttons)
                await event.answer("已刷新")
            elif kind == "rmy" and role == "owner":
                lib, fav = await self.worker.remove_album(int(arg))
                await event.edit(f"已删除 JM{arg}：书库帖子 {lib} 条，收藏帖子 {fav} 条。")
                await event.answer()
            elif kind == "x":
                await event.edit("已取消。")
                await event.answer()
            else:
                await event.answer()
        except Exception as e:
            log.exception("处理按钮失败")
            await event.answer(f"出错了：{clip(str(e) or type(e).__name__, 150)}", alert=True)
