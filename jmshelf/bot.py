"""Bot 私聊：搜索、作品详情（带封面）、入库、收藏、批量、JM 收藏夹同步、任务、状态、用户管理。
只响应白名单用户。"""
import importlib.metadata
import json
import logging
import shutil
import time
from urllib.parse import quote

from telethon import Button, events, functions, types
from telethon.errors import MessageNotModifiedError

from . import ids, sync, ui
from .fmt import clip, esc, size_text, text_table

log = logging.getLogger(__name__)

PER_VIEW = 10

HELP = """<b>JMComic 书库</b>

直接发送：
JM 号：查看详情、入库、收藏。支持 1485198、JM1485198、#JM1485198、禁漫/车号 1485198、全角数字、作品链接
多个 JM 号（空格、逗号或换行隔开）：批量入库或收藏
其他文字：按关键词搜索
/author 作者名：按作者搜索（作品详情里也可以直接点作者）
更多搜索方式（作品、标签、角色、排序、排行榜、分类浏览）在书架的“搜索”页

收藏：作品放进收藏夹就算收藏，会自动入库并生成 PDF 发到精选频道。收藏夹在书架里管理。

命令：
/library  打开书架
/jobs  查看任务
/help  使用说明

所有者命令：
/status  运行状态和版本
/jmsync  同步 JM 账号收藏夹
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
        self._multi: dict[int, list[int]] = {}
        self._jm_data: dict[int, dict] = {}
        self._authors: dict[int, dict] = {}
        self._photos: dict[int, object] = {}

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

    async def _album(self, jm_id: int, fresh: bool = False) -> dict:
        return await self.jm.album(jm_id, fresh)

    # ---- 消息 ----
    async def on_message(self, event):
        uid = event.sender_id
        role = self._role(uid)
        if role is None:
            log.info("忽略非白名单用户 %s", uid)
            return
        if not self.db.user(uid)["name"]:
            sender = await event.get_sender()
            self.db.set_user_name(uid, " ".join(filter(None, [sender.first_name, sender.last_name])))
        text = (event.raw_text or "").strip()
        if not text:
            return
        try:
            if text.startswith("/"):
                cmd, _, arg = text[1:].partition(" ")
                await self._command(event, uid, role, cmd.split("@")[0].lower(), arg.strip())
                return
            found = ids.extract(text)
            if len(found) == 1:
                await self._send_album(event, await self._resolve(found[0]), uid)
            elif found:
                await self._multi_view(event, uid, found)
            else:
                await self._search(event, uid, text, 1, edit=False)
        except Exception as e:
            log.exception("处理消息失败")
            await event.respond(f"出错了：{esc(clip(str(e) or type(e).__name__, 200))}", parse_mode="html")

    async def _resolve(self, item: tuple[str, int]) -> int:
        kind, number = item
        return await self.jm.photo_album(number) if kind == "photo" else number

    async def _command(self, event, uid, role, cmd, arg):
        owner_only = ("users", "adduser", "deluser", "remove", "status", "jmsync")
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
                button = ui.webapp_button(self.cfg, "打开书架搜索页", "?tab=search")
                await event.respond("用法：/search 关键词。书架的搜索页支持按作品、作者、标签、角色搜索，还能排序、看排行榜。",
                                    buttons=[[button]] if button else None)
        elif cmd == "author":
            if arg:
                await self._author_search(event, arg)
            else:
                await event.respond("用法：/author 作者名")
        elif cmd == "preview":
            # 新版搜索页预览：只有从这个按钮打开时才用新版，平时打开书架仍是当前版本
            button = ui.webapp_button(self.cfg, "打开新版搜索页（预览）", "?tab=search&ui=2")
            await event.respond("新版搜索页预览。平时从“书架”菜单打开的仍是当前版本。",
                                buttons=[[button]] if button else None)
        elif cmd == "jobs":
            text, buttons = self._jobs_view()
            await event.respond(text, parse_mode="html", buttons=buttons)
        elif cmd in owner_only and role != "owner":
            await event.respond("这个命令只有所有者能用。")
        elif cmd == "status":
            await event.respond(self._status_view(), parse_mode="html")
        elif cmd == "jmsync":
            await self._jmsync_start(event, uid)
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
            found = ids.extract(arg)
            album = self.db.album(found[0][1]) if found and found[0][0] == "album" else None
            if not album:
                await event.respond("用法：/remove JM号（只能删除已入库的作品）")
                return
            jm_id = album["jm_id"]
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
            state = {"query": query, "pages": {}, "total": 0, "page_size": 0}
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
        rows = self._number_rows([it["jm_id"] for it in items])
        nav = []
        if view > 1:
            nav.append(Button.inline("上一页", f"sp:{view - 1}"))
        if view < views:
            nav.append(Button.inline("下一页", f"sp:{view + 1}"))
        if nav:
            rows.append(nav)
        full = ui.webapp_button(self.cfg, "在书架里搜索（可排序）", f"?search={quote(query)}")
        if full:
            rows.append([full])
        text = "\n".join(lines)
        if edit:
            await event.edit(text, parse_mode="html", buttons=rows)
        else:
            await event.respond(text, parse_mode="html", buttons=rows)

    @staticmethod
    def _number_rows(jm_ids: list[int], offset: int = 0) -> list:
        numbers = [Button.inline(str(offset + i), f"a:{jm_id}") for i, jm_id in enumerate(jm_ids, 1)]
        return [numbers[k:k + 5] for k in range(0, len(numbers), 5)]

    # ---- 按作者搜索：每次 10 条，点“加载更多”再发 10 条 ----
    async def _author_search(self, event, name: str):
        key = (max(self._authors) + 1) if self._authors else 1
        self._authors[key] = {"query": name, "by": "author", "pages": {}, "total": 0, "page_size": 0}
        for old in sorted(self._authors)[:-30]:  # 只保留最近 30 次作者搜索
            del self._authors[old]
        text, rows = await self._author_view(key, 0, more=True)
        await event.respond(text, parse_mode="html", buttons=rows, link_preview=False)

    async def _author_view(self, key: int, start: int, more: bool):
        state = self._authors[key]
        items = await self._search_items(state, start, PER_VIEW)
        name = esc(state["query"])
        if not items:
            return f"没有找到作者“{name}”的作品。", None
        total = state["total"]
        archived = self.db.archived_ids([it["jm_id"] for it in items])
        lines = [f"作者：<b>{name}</b>，共 {total} 本（第 {start + 1}–{start + len(items)} 本）", ""]
        for i, it in enumerate(items, start + 1):
            mark = "  [已入库]" if it["jm_id"] in archived else ""
            lines.append(f"{i}. JM{it['jm_id']}  {esc(clip(it['title'], 60))}{mark}")
        rows = self._number_rows([it["jm_id"] for it in items], start)
        if more and start + len(items) < total:
            rows.append([Button.inline("加载更多", f"am:{key}:{start + len(items)}")])
        full = ui.webapp_button(self.cfg, "在书架里搜索（可排序）", f"?search={quote(state['query'])}&by=author")
        if start == 0 and full:
            rows.append([full])
        return "\n".join(lines), rows

    async def _search_items(self, state, start, count):
        """JM 接口一页条数较多，这里按 10 条一屏切分，并缓存已取过的接口页。"""
        by = state.get("by", "site")
        if not state["pages"]:
            first = await self.jm.search(state["query"], 1, by)
            state["pages"][1] = first["items"]
            state["total"] = first["total"]
            state["page_size"] = len(first["items"]) or 1
        size = state["page_size"]
        result = []
        index = start
        while len(result) < count and index < state["total"]:
            api_page = index // size + 1
            if api_page not in state["pages"]:
                state["pages"][api_page] = (await self.jm.search(state["query"], api_page, by))["items"]
            items = state["pages"][api_page]
            offset = index - (api_page - 1) * size
            if offset >= len(items):
                break
            result.append(items[offset])
            index += 1
        return result

    # ---- 作品 ----
    async def _album_view(self, jm_id: int, uid: int, fresh: bool = False):
        try:
            album = await self._album(jm_id, fresh)
        except Exception as e:
            log.info("取 JM%s 详情失败：%s", jm_id, e)
            return None, f"找不到 JM{jm_id}，或者暂时取不到详情。", None
        text, buttons = ui.album_view(self.cfg, self.db, self.storage, album, uid)
        return album, text, buttons

    async def _send_album(self, event, jm_id: int, uid: int):
        album, text, buttons = await self._album_view(jm_id, uid)
        if album is None:
            await event.respond(text)
            return
        photo = self._photos.get(jm_id)
        if photo is None and await self.worker.ensure_cover(jm_id):
            photo = self.cfg.cover_dir / f"{jm_id}.jpg"
        if photo is None:
            await event.respond(text, parse_mode="html", buttons=buttons, link_preview=False)
            return
        msg = await self.client.send_file(event.chat_id, photo, caption=text, parse_mode="html", buttons=buttons)
        if getattr(msg, "photo", None):
            self._photos[jm_id] = msg.photo

    async def _refresh_album(self, event, jm_id: int, uid: int, fresh: bool = False):
        _, text, buttons = await self._album_view(jm_id, uid, fresh)
        await event.edit(text, parse_mode="html", buttons=buttons, link_preview=False)

    # ---- 多个 JM 号 ----
    async def _multi_view(self, event, uid, found):
        note = await event.respond(f"识别到 {len(found)} 个 JM 号，正在读取详情…")
        lines, jm_ids = [], []
        for item in found:
            try:
                jm_id = await self._resolve(item)
                album = await self._album(jm_id)
            except Exception:
                lines.append(f"{len(lines) + 1}. {'JM' if item[0] == 'album' else '章节 '}{item[1]}  取不到详情")
                continue
            if jm_id in jm_ids:
                continue
            jm_ids.append(jm_id)
            if self.db.fully_archived(jm_id):
                mark = "  [已入库]"
            elif self.db.active_job("archive", jm_id):
                mark = "  [入库中]"
            else:
                mark = ""
            if self.db.is_favorited(uid, jm_id):
                mark += "  [已收藏]"
            lines.append(f"{len(lines) + 1}. JM{jm_id}  {esc(clip(album['title'], 50))}{mark}")
        self._multi[uid] = jm_ids
        todo = sum(1 for j in jm_ids if not self.db.fully_archived(j) and not self.db.active_job("archive", j))
        rows = self._number_rows(jm_ids)
        actions = []
        if todo:
            actions.append(Button.inline(f"全部入库（{todo} 本）", "ba:0"))
        if jm_ids:
            actions.append(Button.inline("全部收藏", "bf:0"))
        if actions:
            rows.append(actions)
        await note.edit("\n".join([f"识别到 {len(found)} 个 JM 号：", ""] + lines), parse_mode="html",
                        buttons=rows or None)

    async def _multi_action(self, event, uid, favorite: bool):
        jm_ids = self._multi.get(uid)
        if not jm_ids:
            await event.answer("列表已过期，请重新发送 JM 号", alert=True)
            return
        if favorite:
            batch = await self.worker.favorite_many(uid, jm_ids)
            await event.answer("已全部收藏" + ("，进度见新消息" if batch else ""))
        else:
            queued = await self.worker.archive_many(uid, jm_ids)
            await event.answer("已加入队列，进度见新消息" if queued else "都已经入库或正在入库")

    # ---- JM 收藏夹同步 ----
    async def _jmsync_start(self, event, uid):
        if not self.cfg.jm_username or not self.cfg.jm_password:
            await event.respond(
                "还没有配置 JM 账号。请在服务器上运行：\n"
                "<code>sudo micro /etc/jmcomic/jmcomic.env</code>\n"
                "填写 JM_USERNAME 和 JM_PASSWORD，保存后运行 <code>sudo systemctl restart jmcomic</code>。",
                parse_mode="html")
            return
        note = await event.respond("正在登录 JM 并读取收藏夹…")
        try:
            data = await self.jm.favorites(self.cfg.jm_username, self.cfg.jm_password)
        except Exception as e:
            log.warning("读取 JM 收藏夹失败", exc_info=True)
            await note.edit(f"读取 JM 收藏夹失败：{esc(clip(str(e) or type(e).__name__, 200))}", parse_mode="html")
            return
        self._jm_data[uid] = data
        await note.edit(self._jmsync_text(data), parse_mode="html", buttons=self._jmsync_buttons())

    def _jmsync_text(self, data: dict) -> str:
        p = sync.preview(self.db, data)
        lines = [f"<b>JM 收藏夹</b>：共 {p['total']} 本", ""]
        lines += [f"{esc(name)}：{count} 本" for name, count in p["folders"]]
        minutes = round(len(p["archive"]) * 0.75)
        lines += ["", f"同步后需要入库 {len(p['archive'])} 本" + (f"（预计约 {minutes} 分钟）" if minutes else "")
                  + f"，生成 PDF {len(p['pdf'])} 本。",
                  "同步只改“JM·”开头的收藏夹，不影响你自建的收藏夹，也不删除书库里的作品。"]
        last = int(self.db.get_meta("jmsync_at", "0"))
        if last:
            lines.append("上次同步：" + time.strftime("%Y-%m-%d %H:%M", time.localtime(last)))
        lines += ["", "反向同步：" + self._reverse_state(),
                  "开启后，你在这边收藏作品时，会自动加进 JM 账号的收藏。只加不删；"
                  "JM 接口只能放进默认收藏；已经在 JM 收藏里的会跳过。"]
        return "\n".join(lines)

    def _reverse_state(self) -> str:
        state = "已开启" if self.db.get_meta("jm_reverse") == "1" else "已关闭"
        try:
            last = json.loads(self.db.get_meta("jm_reverse_last", "{}"))
        except ValueError:
            last = {}
        if last:
            when = time.strftime("%m-%d %H:%M", time.localtime(last["at"]))
            state += f"（最近一次 {when}：加入 {last['added']}，已存在 {last['skipped']}，失败 {last['failed']}）"
        return state

    def _jmsync_buttons(self):
        auto = self.db.get_meta("jmsync_auto") == "1"
        reverse = self.db.get_meta("jm_reverse") == "1"
        return [[Button.inline("开始同步", "js:go"), Button.inline("取消", "x:0")],
                [Button.inline("每天自动同步：已开启（点此关闭）" if auto else "每天自动同步：已关闭（点此开启）", "js:auto")],
                [Button.inline("反向同步到 JM：已开启（点此关闭）" if reverse else "反向同步到 JM：已关闭（点此开启）", "js:rev")]]

    # ---- 任务 / 用户 / 状态 ----
    def _jobs_view(self):
        jobs = self.db.recent_jobs(10)
        if not jobs:
            return "还没有任务。", None
        rows = [[j["id"], j["jm_id"], j["kind"], j["status"]] for j in jobs]
        lines = ["<b>最近的任务</b>", text_table(["ID", "JM", "TYPE", "STATUS"], rows, right={0, 1})]
        for j in jobs:
            if j["status"] in ("running", "queued") and j["step"]:
                lines.append(f"任务 {j['id']}：{esc(j['step'])}")
            elif j["status"] == "failed" and j["error"]:
                lines.append(f"任务 {j['id']} 失败：{esc(clip(j['error'], 120))}")
        return "\n".join(lines), [[Button.inline("刷新", "jobs:0")]]

    def _users_view(self):
        rows = [[u["user_id"], u["role"], time.strftime("%Y-%m-%d", time.localtime(u["added_at"]))]
                for u in self.db.users()]
        names = [f"{u['user_id']}：{esc(u['name'] or '（未知）')}" for u in self.db.users()]
        return "<b>用户</b>\n" + text_table(["USER_ID", "ROLE", "ADDED"], rows, right={0}) + "\n" + "\n".join(names)

    def _status_view(self):
        def version(pkg):
            try:
                return importlib.metadata.version(pkg)
            except importlib.metadata.PackageNotFoundError:
                return "-"

        cache = sum(p.stat().st_size for p in self.cfg.cache_dir.rglob("*") if p.is_file())
        meta_dir = self.cfg.data_dir / "meta"
        rows = [
            ["jmcomic", version("jmcomic")],
            ["telethon", version("telethon")],
            ["disk free", size_text(shutil.disk_usage(self.cfg.data_dir).free)],
            ["page cache", size_text(cache)],
            ["albums", len(self.db.albums(self.cfg.owner_id))],
            ["meta files", len(list(meta_dir.glob("JM*.json"))) if meta_dir.exists() else 0],
            ["queue", self.db.queued_count()],
        ]
        lines = ["<b>运行状态</b>", text_table(["ITEM", "VALUE"], rows, right={1})]
        status_file = self.cfg.data_dir / "update-status.json"
        if status_file.exists():
            try:
                s = json.loads(status_file.read_text())
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("checked_at", 0)))
                lines.append(f"自动更新：每天 04:30 检查；最近一次 {when}，{esc(s.get('message', ''))}")
            except ValueError:
                pass
        else:
            lines.append("自动更新：每天 04:30 检查；还没有运行过。")
        if self.cfg.jm_username:
            auto = "已开启" if self.db.get_meta("jmsync_auto") == "1" else "已关闭"
            lines.append(f"JM 收藏夹同步：已配置账号，每天自动同步{auto}。")
            lines.append("反向同步到 JM：" + esc(self._reverse_state()) + "。")
        else:
            lines.append("JM 收藏夹同步：未配置账号。")
        return "\n".join(lines)

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
                await self._send_album(event, int(arg), uid)
            elif kind == "r":
                await self._refresh_album(event, int(arg), uid, fresh=True)
                await event.answer("已刷新")
            elif kind == "sp":
                state = self._searches.get(uid)
                if not state:
                    await event.answer("搜索已过期，请重新发送关键词", alert=True)
                    return
                await event.answer()
                await self._search(event, uid, state["query"], int(arg), edit=True)
            elif kind == "au":
                jm_id, _, index = arg.partition(":")
                album = await self._album(int(jm_id))
                await event.answer()
                await self._author_search(event, album["authors"][int(index)])
            elif kind == "am":
                key, _, start = arg.partition(":")
                if int(key) not in self._authors:
                    await event.answer("列表已过期，请重新搜索作者", alert=True)
                    return
                start = int(start)
                # 旧消息按原样重写，只去掉“加载更多”，再发一条新消息放接下来的 10 条
                text, rows = await self._author_view(int(key), start - PER_VIEW, more=False)
                await event.edit(text, parse_mode="html", buttons=rows, link_preview=False)
                await event.answer()
                text, rows = await self._author_view(int(key), start, more=True)
                await event.respond(text, parse_mode="html", buttons=rows, link_preview=False)
            elif kind == "ar":
                status, _ = await self.worker.submit("archive", int(arg), uid)
                await event.answer("已加入队列" if status == "created" else "已经在队列里了")
                await self._refresh_album(event, int(arg), uid)
            elif kind == "fv":
                result = await self.worker.favorite(uid, int(arg))
                await event.answer({"archive": "已收藏，正在入库，完成后自动生成 PDF",
                                    "pdf": "已收藏，正在生成 PDF"}.get(result, "已收藏"))
                await self._refresh_album(event, int(arg), uid)
            elif kind == "uf":
                self.db.unfavorite(uid, int(arg))
                self.worker.save_meta(int(arg))
                await event.answer("已取消收藏。精选频道里的 PDF 保留不删", alert=True)
                await self._refresh_album(event, int(arg), uid)
            elif kind == "ba":
                await self._multi_action(event, uid, favorite=False)
            elif kind == "bf":
                await self._multi_action(event, uid, favorite=True)
            elif kind == "js" and role == "owner":
                await self._jmsync_callback(event, uid, arg)
            elif kind == "jobs":
                text, buttons = self._jobs_view()
                await event.edit(text, parse_mode="html", buttons=buttons)
                await event.answer("已刷新")
            elif kind == "rmy" and role == "owner":
                lib, pdf = await self.worker.remove_album(int(arg))
                await event.edit(f"已删除 JM{arg}：书库帖子 {lib} 条，PDF 帖子 {pdf} 条。")
                await event.answer()
            elif kind == "x":
                await event.edit("已取消。")
                await event.answer()
            else:
                await event.answer()
        except MessageNotModifiedError:
            await event.answer("已是最新")
        except Exception as e:
            log.exception("处理按钮失败")
            await event.answer(f"出错了：{clip(str(e) or type(e).__name__, 150)}", alert=True)

    async def _jmsync_callback(self, event, uid, arg):
        if arg in ("auto", "rev"):
            key, name = ("jmsync_auto", "每天自动同步") if arg == "auto" else ("jm_reverse", "反向同步到 JM")
            on = self.db.get_meta(key) != "1"
            self.db.set_meta(key, "1" if on else "0")
            await event.answer(f"已开启{name}" if on else f"已关闭{name}")
            data = self._jm_data.get(uid)
            if data:
                await event.edit(self._jmsync_text(data), parse_mode="html", buttons=self._jmsync_buttons())
            return
        data = self._jm_data.pop(uid, None)
        if data is None:
            await event.answer("数据已过期，请重新发送 /jmsync", alert=True)
            return
        await event.answer("开始同步")
        plan, batch = await self.worker.jm_sync(uid, data)
        await event.edit("同步完成：" + sync.summary_counts(plan) + ("。入库和 PDF 进度见新消息。" if batch else "。"))
