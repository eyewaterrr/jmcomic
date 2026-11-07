"""任务队列和后台工作（v3）：

- publish（上传精选）：取作品全部章节 → 从缓存取原图，缺的从 JM 补下 → 子进程还原并合成 PDF
  → 并行上传精选频道。这是唯一的任务类型，串行执行。
- 收藏只改收藏夹，不建任务；新收藏、读过的作品在后台补元数据和封面（书架显示用）。
- 开启后每 24 小时同步一次 JM 账号收藏夹（只镜像收藏夹）。

数据库备份不在这里做：服务器的每周 R2 备份已经包含数据库快照和配置。
"""
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from PIL import Image
from telethon import Button
from telethon.errors import MessageDeleteForbiddenError, MessageNotModifiedError

from . import images, meta, sync, ui
from .fmt import clip, esc, size_text

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MIN_FREE_BYTES = 1536 * 1024 * 1024
STATUS_INTERVAL = 3
SYNC_INTERVAL = 24 * 3600
UPDATE_REQUEST_INTERVAL = 6 * 3600
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class JobError(Exception):
    """不需要重试的错误，原样告诉用户。"""


def _file_name(text: str) -> str:
    return "".join("_" if ch in '/\\:*?"<>|\n\r\t' else ch for ch in text).strip()


class Worker:
    def __init__(self, cfg, db, jm, storage, client, bot_username: str, cache=None):
        self.cfg = cfg
        self.db = db
        self.jm = jm
        self.storage = storage
        self.client = client
        self.bot_username = bot_username
        self.cache = cache
        self._wake = asyncio.Event()
        self._last_status: dict[int, float] = {}
        self._push_lock = asyncio.Lock()
        self._meta_queue: asyncio.Queue | None = None
        self._meta_pending: set[int] = set()
        self.live: dict[int, dict] = {}  # 任务 ID → {"percent", "text"}，作品页的进度条用

    # ---- 上传精选 ----
    async def submit(self, kind: str, jm_id: int, user_id: int) -> tuple[str, int]:
        """建任务并在私聊里发一条进度消息。返回 (状态, 任务 ID)，状态为 created / exists。"""
        active = self.db.active_job(kind, jm_id)
        if active:
            return "exists", active["id"]
        msg = await self.client.send_message(user_id, f"JM{jm_id} {ui.KIND_NAME[kind]}：已加入队列")
        job_id, _ = self.db.add_job(kind, jm_id, user_id, user_id, msg.id)
        self._wake.set()
        return "created", job_id

    async def publish(self, user_id: int, jm_id: int) -> str:
        """返回 done（已经上传过）/ created / exists。"""
        if self.db.pick(jm_id):
            return "done"
        status, _ = await self.submit("publish", jm_id, user_id)
        return status

    # ---- 收藏（只改收藏夹） ----
    async def favorite(self, user_id: int, jm_id: int):
        was = self.db.is_favorited(user_id, jm_id)
        self.db.add_to_folder(self.db.default_folder(user_id), jm_id)
        self.remember(jm_id)
        self.save_meta(jm_id)
        if not was:
            self.queue_jm_push(user_id, [jm_id])

    async def favorite_many(self, user_id: int, jm_ids: list[int]):
        jm_ids = list(dict.fromkeys(jm_ids))
        self.queue_jm_push(user_id, [j for j in jm_ids if not self.db.is_favorited(user_id, j)])
        folder = self.db.default_folder(user_id)
        for jm_id in jm_ids:
            self.db.add_to_folder(folder, jm_id)
            self.remember(jm_id)
            self.save_meta(jm_id)

    def queue_jm_push(self, user_id: int, jm_ids: list[int]):
        """反向同步：所有者在这边收藏时，也加进 JM 账号的收藏。开关在 /jmsync，默认关闭；只加不删。"""
        if (user_id != self.cfg.owner_id or not jm_ids or not self.cfg.jm_username
                or self.db.get_meta("jm_reverse") != "1"):
            return
        asyncio.get_running_loop().create_task(self._push_jm(list(jm_ids)))

    async def _push_jm(self, jm_ids: list[int]):
        async with self._push_lock:
            try:
                result = await self.jm.push_favorites(self.cfg.jm_username, self.cfg.jm_password, jm_ids)
            except Exception as e:
                result = {"added": [], "skipped": [], "failed": [(i, clip(str(e) or type(e).__name__, 120)) for i in jm_ids]}
            self.db.set_meta("jm_reverse_last", json.dumps({
                "at": int(time.time()), "added": len(result["added"]),
                "skipped": len(result["skipped"]), "failed": len(result["failed"])}))
            log.info("反向同步到 JM：加入 %s，已在 JM 收藏 %s，失败 %s",
                     result["added"], result["skipped"], result["failed"])
            if result["failed"]:
                lines = [f"JM{i}：{esc(err)}" for i, err in result["failed"][:10]]
                try:
                    await self.client.send_message(self.cfg.owner_id, "反向同步到 JM 失败：\n" + "\n".join(lines),
                                                   parse_mode="html")
                except Exception:
                    log.warning("发送反向同步失败通知失败", exc_info=True)

    def save_meta(self, jm_id: int):
        try:
            meta.write_album(self.db, self.cfg.data_dir, jm_id)
        except Exception:
            log.warning("写 JM%s 元数据文件失败", jm_id, exc_info=True)

    # ---- 作品元数据（书架显示用） ----
    def remember(self, jm_id: int, album: dict | None = None):
        """记下这本作品：已有详情就直接写进数据库，否则先占位，由后台慢慢补（一次一本，不扎堆请求 JM）。"""
        if album is not None:
            self.db.upsert_album(album)
            self.save_meta(jm_id)
            if not (self.cfg.cover_dir / f"{jm_id}.jpg").exists():
                self._queue_meta(jm_id)
            return
        self.db.ensure_album_stub(jm_id)
        row = self.db.album(jm_id)
        if not row["title"] or not (self.cfg.cover_dir / f"{jm_id}.jpg").exists():
            self._queue_meta(jm_id)

    def _queue_meta(self, jm_id: int):
        if self._meta_queue is None:
            self._meta_queue = asyncio.Queue()
        if jm_id not in self._meta_pending:
            self._meta_pending.add(jm_id)
            self._meta_queue.put_nowait(jm_id)

    async def _meta_loop(self):
        for jm_id in self.db.stub_albums():
            self._queue_meta(jm_id)
        if self._meta_queue is None:
            self._meta_queue = asyncio.Queue()
        while True:
            jm_id = await self._meta_queue.get()
            try:
                row = self.db.album(jm_id)
                if row is None or not row["title"]:
                    self.db.upsert_album(await self.jm.album(jm_id))
                    self.save_meta(jm_id)
                await self.ensure_cover(jm_id)
            except Exception as e:
                log.info("补 JM%s 的元数据失败：%s", jm_id, e)
            finally:
                self._meta_pending.discard(jm_id)
            await asyncio.sleep(0.5)

    # ---- 主循环 ----
    async def run(self):
        self.db.requeue_running()
        for d in (self.cfg.work_dir, self.cfg.cover_dir, self.cfg.cache_dir):
            d.mkdir(parents=True, exist_ok=True)
        side = [asyncio.create_task(self._sync_loop()), asyncio.create_task(self._meta_loop())]
        try:
            while True:
                job = self.db.next_job()
                if job is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._process(job)
        finally:
            for t in side:
                t.cancel()

    async def _process(self, job):
        attempt = job["attempts"] + 1
        self.db.update_job(job["id"], status="running", attempts=attempt, error="")
        workdir = self.cfg.work_dir / f"job{job['id']}"
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True)
        try:
            free = shutil.disk_usage(self.cfg.data_dir).free
            if free < MIN_FREE_BYTES:
                raise JobError(f"服务器磁盘只剩 {size_text(free)}，暂停接新任务")
            if job["kind"] == "publish":
                await self._publish(job, workdir)
            else:
                raise JobError(f"未知任务类型 {job['kind']}")
            self.db.update_job(job["id"], status="done", step="")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("任务 %s 失败", job["id"])
            reason = clip(str(e) or type(e).__name__, 300)
            if not isinstance(e, JobError) and attempt < MAX_ATTEMPTS:
                self.db.update_job(job["id"], status="queued", error=reason,
                                   run_after=int(time.time()) + 60 * attempt)
                await self._status(job, f"出错，{attempt} 分钟后自动重试（第 {attempt}/{MAX_ATTEMPTS} 次）：{esc(reason)}", force=True)
            else:
                self.db.update_job(job["id"], status="failed", error=reason)
                await self._status(job, f"失败：{esc(reason)}", force=True)
        finally:
            self.live.pop(job["id"], None)
            shutil.rmtree(workdir, ignore_errors=True)

    async def _status(self, job, text: str, force: bool = False, buttons=None):
        now = time.monotonic()
        if not force and now - self._last_status.get(job["id"], 0) < STATUS_INTERVAL:
            return
        self._last_status[job["id"]] = now
        self.db.update_job(job["id"], step=clip(text, 200))
        row = self.db.job(job["id"])
        if not row["chat_id"]:
            return
        body = f"JM{row['jm_id']} {ui.KIND_NAME.get(row['kind'], row['kind'])}：{text}"
        await self._edit_or_send(row["chat_id"], row["msg_id"], body, buttons,
                                 lambda mid: self.db.update_job(job["id"], msg_id=mid))

    async def _edit_or_send(self, chat_id, msg_id, body, buttons, remember):
        try:
            if msg_id:
                await self.client.edit_message(chat_id, msg_id, body, parse_mode="html",
                                               buttons=buttons, link_preview=False)
            else:
                msg = await self.client.send_message(chat_id, body, parse_mode="html",
                                                     buttons=buttons, link_preview=False)
                remember(msg.id)
        except MessageNotModifiedError:
            pass
        except Exception:
            log.warning("更新进度消息失败", exc_info=True)

    def _progress(self, job, label: str, span: tuple[float, float] | None = None):
        """上传进度回调：更新私聊进度消息；给了 span（起点, 终点，0–1）时同时更新进度条。"""
        def callback(done, total):
            frac = done / max(total, 1)
            if span:
                self._set_live(job, span[0] + (span[1] - span[0]) * frac, f"{label}上传 {frac * 100:.0f}%")
            if time.monotonic() - self._last_status.get(job["id"], 0) >= STATUS_INTERVAL:
                asyncio.get_running_loop().create_task(
                    self._status(job, f"{label}上传 {frac * 100:.0f}%（{size_text(total)}）"))
        return callback

    def _set_live(self, job, fraction: float, text: str):
        self.live[job["id"]] = {"percent": max(0, min(100, round(fraction * 100))), "text": text}

    def progress_for(self, kind: str, jm_id: int) -> dict | None:
        """某作品进行中的任务进度：正在跑的给百分比，排队中的给前面还有几个任务。"""
        job = self.db.active_job(kind, jm_id)
        if job is None:
            return None
        if job["status"] == "running":
            return dict(self.live.get(job["id"], {"percent": 0, "text": "准备中"}), queued=False)
        ahead = self.db.jobs_ahead(job["id"])
        return {"percent": 0, "text": f"排队中，前面还有 {ahead} 个任务" if ahead else "排队中", "queued": True}

    async def _run_module(self, args: list[str], cwd: Path, timeout: int) -> str:
        env = dict(os.environ, PYTHONPATH=str(PACKAGE_ROOT))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", *args, cwd=str(cwd), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            proc.kill()
            await proc.wait()
            if isinstance(e, asyncio.CancelledError):
                raise
            raise RuntimeError(f"{args[0]} 超时")
        if proc.returncode != 0:
            tail = err.decode(errors="replace").strip().splitlines()[-3:]
            raise RuntimeError(f"{args[0]} 出错：{' | '.join(tail)}")
        return out.decode().strip().splitlines()[-1]

    def request_update_check(self):
        """从 JM 下载出错可能是 JM 改了接口：请求自动更新服务检查一次 jmcomic 新版本（6 小时内最多一次）。"""
        last = int(self.db.get_meta("update_request_at", "0"))
        if time.time() - last < UPDATE_REQUEST_INTERVAL:
            return
        try:
            (self.cfg.data_dir / "update-request").write_text(str(int(time.time())))
            self.db.set_meta("update_request_at", str(int(time.time())))
            log.info("已请求检查 jmcomic 更新")
        except OSError:
            log.warning("写更新请求文件失败", exc_info=True)

    # ---- 封面 ----
    async def ensure_cover(self, jm_id: int) -> Path | None:
        web = self.cfg.cover_dir / f"{jm_id}.jpg"
        thumb = self.cfg.cover_dir / f"{jm_id}_thumb.jpg"
        if web.exists() and thumb.exists():
            return thumb
        self.cfg.cover_dir.mkdir(parents=True, exist_ok=True)
        raw = self.cfg.cover_dir / f"{jm_id}_raw.jpg"  # jmcomic 按扩展名决定保存格式
        try:
            await self.jm.cover(jm_id, raw)
            await asyncio.to_thread(self._make_covers, raw, web, thumb)
            return thumb
        except Exception:
            log.warning("JM%s 封面下载失败", jm_id, exc_info=True)
            return None
        finally:
            raw.unlink(missing_ok=True)

    @staticmethod
    def _make_covers(raw: Path, web: Path, thumb: Path):
        with Image.open(raw) as im:
            im = im.convert("RGB")
            big = im.copy()
            big.thumbnail((480, 960))
            big.save(web, "JPEG", quality=85)
            im.thumbnail((320, 320))
            im.save(thumb, "JPEG", quality=80)

    def _caption(self, title: str, jm_id: int, detail: str) -> str:
        # 只放给人看的内容；JM 号、卷号等机器信息在 PDF 元数据里
        return f"<b>[JM{jm_id}] {esc(clip(title, 300))}</b>\n{esc(detail)}\n#JM{jm_id}"

    def _read_button(self, param: str):
        if not self.cfg.public_url:
            return None
        return [Button.url("在线阅读", ui.startapp_link(self.bot_username, param))]

    async def _publish(self, job, workdir: Path):
        jm_id = job["jm_id"]
        if self.db.pick(jm_id):
            await self._status(job, "已经上传过了", force=True)
            return
        self._set_live(job, 0, "获取作品信息")
        await self._status(job, "获取作品信息", force=True)
        try:
            album = await self.jm.album(jm_id, fresh=True)
            details = [await self.jm.chapter(c["photo_id"]) for c in album["chapters"]]
        except Exception:
            self.request_update_check()
            raise
        self.remember(jm_id, album)
        thumb = await self.ensure_cover(jm_id)
        chapters = album["chapters"]
        total = sum(len(d["pages"]) for d in details)
        if not total:
            raise JobError("JM 上这本没有图片")

        pages: list[list] = []
        with self.cache.lock(c["photo_id"] for c in chapters):
            before = 0
            for i, (ch, d) in enumerate(zip(chapters, details), 1):
                self.db.set_chapter_pages(ch["photo_id"], len(d["pages"]))
                label = f"第 {i}/{len(chapters)} 章，" if len(chapters) > 1 else ""

                def progress(done, count, base=before, label=label):
                    text = f"{label}准备图片 {done}/{count} 页"
                    self._set_live(job, 0.6 * (base + done) / total, text)
                    if time.monotonic() - self._last_status.get(job["id"], 0) >= STATUS_INTERVAL:
                        asyncio.get_running_loop().create_task(self._status(job, text))

                try:
                    paths = await self.cache.fill(ch["photo_id"], d, progress)
                except Exception:
                    self.request_update_check()
                    raise
                pages += [[str(p), info["num"]] for p, info in zip(paths, d["pages"])]
                before += len(d["pages"])

            self._set_live(job, 0.62, f"生成 PDF（{total} 页）")
            await self._status(job, f"生成 PDF（{total} 页）", force=True)
            authors = album["authors"]
            spec = {
                "jm_id": jm_id,
                "out_dir": str(workdir / "pdf"),
                "name": _file_name(f"[JM{jm_id}] {clip(album['title'], 80)}"),
                "title": album["title"], "author": ", ".join(authors),
                "quality": self.cfg.pdf_quality, "volume_bytes": self.cfg.pdf_volume_mb * 1024 * 1024,
                "pages": pages,
            }
            spec_path = workdir / "pdf.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False))
            result = json.loads(await self._run_module(["jmshelf.pdf", str(spec_path)], cwd=workdir, timeout=3600))

        volumes = result["volumes"]
        msg_ids, shas, total_size = [], [], 0
        for k, vol in enumerate(volumes, 1):
            path = Path(vol["path"])
            size = path.stat().st_size
            sha = await asyncio.to_thread(images.file_sha256, path)
            label = f"卷 {k}/{len(volumes)}，" if len(volumes) > 1 else ""
            detail = (f"PDF · {len(chapters)} 章 · {vol['pages']} 页 · {size_text(size)}"
                      + (f" · 卷 {k}/{len(volumes)}" if len(volumes) > 1 else ""))
            span = (0.8 + 0.2 * (k - 1) / len(volumes), 0.8 + 0.2 * k / len(volumes))
            msg = await self.storage.send_document(
                self.storage.fav_channel, path, self._caption(album["title"], jm_id, detail),
                path.name, thumb=thumb, buttons=self._read_button(f"a{jm_id}"),
                progress=self._progress(job, label, span))
            msg_ids.append(msg.id)
            shas.append(sha)
            total_size += size
            path.unlink(missing_ok=True)
        self.db.add_pick(jm_id, msg_ids, total_size, ",".join(shas), job["user_id"])
        self.save_meta(jm_id)
        link = self.storage.post_link(self.storage.fav_channel_id, msg_ids[0])
        await self._status(job, f"完成，PDF {size_text(total_size)}", force=True,
                           buttons=[[Button.url("查看精选", link)]])

    # ---- 删除精选 ----
    async def remove_pick(self, jm_id: int) -> tuple[int, list[str]]:
        """删除精选记录和帖子。返回 (删掉的帖子数, Bot 删不掉、需要手动删的帖子链接)。

        Telegram 不允许 Bot 删除 48 小时以前的频道消息，这些帖子只能在频道里手动删。
        """
        msg_ids = self.db.remove_pick(jm_id)
        self.save_meta(jm_id)
        deleted, manual = 0, []
        for mid in msg_ids:
            try:
                await self.storage.delete(self.storage.fav_channel, [mid])
                deleted += 1
            except MessageDeleteForbiddenError:
                manual.append(self.storage.post_link(self.storage.fav_channel_id, mid))
        return deleted, manual

    # ---- JM 收藏夹同步 ----
    async def jm_sync(self, user_id: int, data: dict | None = None) -> dict:
        """把 JM 收藏夹镜像成本地收藏夹；新出现的作品在后台补元数据和封面。"""
        if data is None:
            data = await self.jm.favorites(self.cfg.jm_username, self.cfg.jm_password)
        plan = sync.apply(self.db, user_id, data)
        for jm_id in plan["new"]:
            self.remember(jm_id)
        for jm_id in plan["changed"]:
            self.save_meta(jm_id)
        self.db.set_meta("jmsync_at", str(int(time.time())))
        return plan

    async def _sync_loop(self):
        while True:
            await asyncio.sleep(600)
            if self.db.get_meta("jmsync_auto") != "1" or not self.cfg.jm_username:
                continue
            if time.time() - int(self.db.get_meta("jmsync_at", "0")) < SYNC_INTERVAL:
                continue
            try:
                plan = await self.jm_sync(self.cfg.owner_id)
                log.info("JM 收藏夹自动同步：%s", sync.summary_counts(plan))
            except Exception as e:
                log.exception("JM 收藏夹自动同步失败")
                self.db.set_meta("jmsync_at", str(int(time.time())))
                try:
                    await self.client.send_message(self.cfg.owner_id, f"JM 收藏夹自动同步失败：{clip(str(e), 200)}")
                except Exception:
                    pass
