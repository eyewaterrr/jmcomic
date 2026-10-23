"""任务队列（串行执行）：

- archive：取详情 → 子进程下载每章 → 打包 CBZ → 并行上传书库频道 → 写索引
- favorite：从书库频道取回各章 → 子进程转 PDF → 上传精选频道（被收藏的作品自动生成）
- 每 24 小时把数据库快照发到书库频道，只保留最近 3 份
- 开启后每 24 小时同步一次 JM 账号收藏夹

批量任务（多个 JM 号、JM 收藏夹同步）共用一条进度消息，不逐本发消息。
"""
import asyncio
import gzip
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image
from telethon import Button
from telethon.errors import MessageNotModifiedError

from . import meta, pack, sync, ui
from .fmt import clip, esc, size_text

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MIN_FREE_BYTES = 1536 * 1024 * 1024
STATUS_INTERVAL = 3
BACKUP_INTERVAL = 24 * 3600
BACKUP_KEEP = 3
SYNC_INTERVAL = 24 * 3600
UPDATE_REQUEST_INTERVAL = 6 * 3600
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class JobError(Exception):
    """不需要重试的错误，原样告诉用户。"""


class Deferred(Exception):
    """前置任务还没完成，稍后再跑，不算失败次数。"""


def _file_name(text: str) -> str:
    return "".join("_" if ch in '/\\:*?"<>|\n\r\t' else ch for ch in text).strip()


class Worker:
    def __init__(self, cfg, db, jm, storage, client, bot_username: str):
        self.cfg = cfg
        self.db = db
        self.jm = jm
        self.storage = storage
        self.client = client
        self.bot_username = bot_username
        self._wake = asyncio.Event()
        self._last_status: dict[int, float] = {}
        self._last_batch: dict[int, float] = {}
        self._push_lock = asyncio.Lock()

    # ---- 提交 ----
    async def submit(self, kind: str, jm_id: int, user_id: int, batch_id: int | None = None) -> tuple[str, int]:
        """建任务。单个任务在私聊里发一条进度消息；批量任务用批次的进度消息。
        返回 (状态, 任务 ID)，状态为 created / exists。"""
        active = self.db.active_job(kind, jm_id)
        if active:
            return "exists", active["id"]
        msg_id = None
        if batch_id is None:
            msg = await self.client.send_message(user_id, f"JM{jm_id} {ui.KIND_NAME[kind]}：已加入队列")
            msg_id = msg.id
        job_id, _ = self.db.add_job(kind, jm_id, user_id, user_id if batch_id is None else None, msg_id, batch_id)
        self._wake.set()
        return "created", job_id

    async def favorite(self, user_id: int, jm_id: int, batch_id: int | None = None) -> str:
        """加入默认收藏夹，并安排后续工作：没入库就先入库，入库完成后自动生成 PDF。"""
        was = self.db.is_favorited(user_id, jm_id)
        self.db.add_to_folder(self.db.default_folder(user_id), jm_id)
        self.save_meta(jm_id)
        if not was:
            self.queue_jm_push(user_id, [jm_id])
        return await self.after_favorite(user_id, jm_id, batch_id)

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

    async def after_favorite(self, user_id: int, jm_id: int, batch_id: int | None = None) -> str:
        if not self.db.fully_archived(jm_id):
            await self.submit("archive", jm_id, user_id, batch_id)
            return "archive"
        if not self.db.pdf_record(jm_id):
            await self.submit("favorite", jm_id, user_id, batch_id)
            return "pdf"
        return "done"

    async def start_batch(self, user_id: int, label: str, archive_ids: list[int], pdf_ids: list[int]) -> int | None:
        """建一个批次：archive_ids 入库（被收藏的入库后自动生成 PDF），pdf_ids 直接生成 PDF。"""
        archive_ids = [i for i in archive_ids if not self.db.active_job("archive", i)]
        pdf_ids = [i for i in pdf_ids if not self.db.active_job("favorite", i)]
        if not archive_ids and not pdf_ids:
            return None
        msg = await self.client.send_message(user_id, f"{label}：已加入队列，入库 {len(archive_ids)} 本，生成 PDF {len(pdf_ids)} 本")
        batch_id = self.db.create_batch(user_id, label, user_id, msg.id)
        for jm_id in archive_ids:
            await self.submit("archive", jm_id, user_id, batch_id)
        for jm_id in pdf_ids:
            await self.submit("favorite", jm_id, user_id, batch_id)
        return batch_id

    def save_meta(self, jm_id: int):
        try:
            meta.write_album(self.db, self.cfg.data_dir, jm_id)
        except Exception:
            log.warning("写 JM%s 元数据文件失败", jm_id, exc_info=True)

    # ---- 主循环 ----
    async def run(self):
        self.db.requeue_running()
        for d in (self.cfg.work_dir, self.cfg.cover_dir, self.cfg.cache_dir):
            d.mkdir(parents=True, exist_ok=True)
        side = [asyncio.create_task(self._backup_loop()), asyncio.create_task(self._sync_loop())]
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
                if job["batch_id"]:
                    await self._batch_status(job["batch_id"])
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
            if job["kind"] == "archive":
                await self._archive(job, workdir)
            elif job["kind"] == "favorite":
                await self._favorite(job, workdir)
            else:
                raise JobError(f"未知任务类型 {job['kind']}")
            self.db.update_job(job["id"], status="done", step="")
        except asyncio.CancelledError:
            raise
        except Deferred as e:
            self.db.update_job(job["id"], status="queued", attempts=job["attempts"], step=str(e),
                               run_after=int(time.time()) + 120)
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
        body = f"JM{row['jm_id']} {ui.KIND_NAME[row['kind']]}：{text}"
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

    async def _batch_status(self, batch_id: int):
        batch = self.db.batch(batch_id)
        if batch is None or batch["finished"]:
            return
        counts = self.db.batch_counts(batch_id)

        def n(kind, *statuses):
            return sum(counts.get((kind, s), 0) for s in statuses)

        active = n("archive", "queued", "running") + n("favorite", "queued", "running")
        now = time.monotonic()
        if active and now - self._last_batch.get(batch_id, 0) < 5:
            return
        self._last_batch[batch_id] = now
        a_total = sum(v for (k, _), v in counts.items() if k == "archive")
        p_total = sum(v for (k, _), v in counts.items() if k == "favorite")
        failed = n("archive", "failed") + n("favorite", "failed")
        text = (f"{esc(batch['label'])}：入库 {n('archive', 'done')}/{a_total}，"
                f"生成 PDF {n('favorite', 'done')}/{p_total}" + (f"，失败 {failed}" if failed else ""))
        if not active:
            self.db.finish_batch(batch_id)
            text = "完成。" + text + ("。失败的任务可以发 /jobs 查看原因" if failed else "")
        await self._edit_or_send(batch["chat_id"], batch["msg_id"], text, None,
                                 lambda mid: self.db.set_batch_msg(batch_id, mid))

    def _progress(self, job, label: str):
        def callback(done, total):
            if time.monotonic() - self._last_status.get(job["id"], 0) >= STATUS_INTERVAL:
                asyncio.get_running_loop().create_task(
                    self._status(job, f"{label}上传 {done * 100 // max(total, 1)}%（{size_text(total)}）"))
        return callback

    async def _run_module(self, args: list[str], cwd: Path, timeout: int) -> str:
        env = dict(os.environ, PYTHONPATH=str(PACKAGE_ROOT))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", *args, cwd=str(cwd), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"{args[0]} 超时")
        if proc.returncode != 0:
            tail = err.decode(errors="replace").strip().splitlines()[-3:]
            raise RuntimeError(f"{args[0]} 出错：{' | '.join(tail)}")
        return out.decode().strip().splitlines()[-1]

    def _request_update_check(self):
        """下载出错可能是 JM 改了接口：请求自动更新服务检查一次 jmcomic 新版本（6 小时内最多一次）。"""
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

    # ---- 入库 ----
    def _caption(self, title: str, jm_id: int, detail: str) -> str:
        # 只放给人看的内容；JM 号、章节号等机器信息在 CBZ 的 ComicInfo.xml 和 PDF 元数据里
        return f"<b>[JM{jm_id}] {esc(clip(title, 300))}</b>\n{esc(detail)}\n#JM{jm_id}"

    def _read_button(self, param: str):
        if not self.cfg.public_url:
            return None
        return [Button.url("在线阅读", ui.startapp_link(self.bot_username, param))]

    async def _archive(self, job, workdir: Path):
        jm_id = job["jm_id"]
        await self._status(job, "获取作品信息", force=True)
        album = await self.jm.album(jm_id)
        self.db.upsert_album(album)
        thumb = await self.ensure_cover(jm_id)
        chapters = album["chapters"]
        total = len(chapters)
        for i, ch in enumerate(chapters, 1):
            if self.db.chapter(ch["photo_id"]):
                continue
            label = f"第 {i}/{total} 章，" if total > 1 else ""
            await self._status(job, f"{label}下载图片", force=True)
            img_dir = workdir / str(ch["photo_id"])
            try:
                await self._run_module(
                    ["jmshelf.fetch", str(ch["photo_id"]), str(img_dir), str(self.cfg.image_threads)],
                    cwd=workdir, timeout=3600)
            except RuntimeError:
                self._request_update_check()
                raise
            images = pack.list_images(img_dir)
            if not images:
                raise RuntimeError("没有下载到图片")

            await self._status(job, f"{label}打包 {len(images)} 页", force=True)
            cbz = workdir / f"{ch['photo_id']}.cbz"
            info = pack.comicinfo_xml(album, ch, len(images))
            pages, sha = await asyncio.to_thread(pack.build_cbz, images, cbz, info)
            shutil.rmtree(img_dir, ignore_errors=True)
            size = cbz.stat().st_size

            if total > 1:
                detail = f"第 {i}/{total} 章 · {ch['title']} · {len(pages)} 页 · {size_text(size)}"
                name = f"[JM{jm_id}] {clip(album['title'], 60)} - {ch['sort']:02d}.cbz"
            else:
                detail = f"{len(pages)} 页 · {size_text(size)}"
                name = f"[JM{jm_id}] {clip(album['title'], 80)}.cbz"
            msg = await self.storage.send_document(
                self.storage.channel, cbz, self._caption(album["title"], jm_id, detail),
                _file_name(name), thumb=thumb, buttons=self._read_button(f"c{ch['photo_id']}"),
                progress=self._progress(job, label))
            self.db.add_chapter(jm_id, ch["photo_id"], ch["sort"], ch["title"], msg.id, size, sha, pages)
            self.save_meta(jm_id)
            cbz.unlink(missing_ok=True)

        # 先标记完成，按钮才不会显示“入库中”
        self.db.update_job(job["id"], status="done")
        self.save_meta(jm_id)
        if self.db.favorited_by_anyone(jm_id) and not self.db.pdf_record(jm_id):
            await self.submit("favorite", jm_id, job["user_id"], job["batch_id"])
        _, buttons = ui.album_view(self.cfg, self.db, self.storage, album, job["user_id"])
        pages = sum(r["page_count"] for r in self.db.chapters(jm_id))
        await self._status(job, f"完成，{total} 章 {pages} 页", force=True, buttons=buttons)

    # ---- 收藏 PDF ----
    async def _favorite(self, job, workdir: Path):
        jm_id = job["jm_id"]
        if self.db.active_job("archive", jm_id):
            raise Deferred("等待入库完成")
        album = self.db.album(jm_id)
        chapters = self.db.chapters(jm_id)
        if not album or len(chapters) < album["chapter_count"]:
            raise JobError("这本还没有完整入库，请先入库")
        if self.db.pdf_record(jm_id):
            await self._status(job, "PDF 已经生成过了", force=True)
            return

        cbz_files = []
        for i, ch in enumerate(chapters, 1):
            await self._status(job, f"取回第 {i}/{len(chapters)} 章", force=True)
            dest = workdir / f"{ch['photo_id']}.cbz"
            await self.storage.download(self.storage.channel, ch["msg_id"], dest)
            cbz_files.append(str(dest))

        await self._status(job, "转换 PDF", force=True)
        authors = json.loads(album["authors"])
        spec = {
            "jm_id": jm_id,
            "out_dir": str(workdir / "pdf"),
            "name": _file_name(f"[JM{jm_id}] {clip(album['title'], 80)}"),
            "title": album["title"], "author": ", ".join(authors),
            "quality": self.cfg.pdf_quality, "volume_bytes": self.cfg.pdf_volume_mb * 1024 * 1024,
            "cbz": cbz_files,
        }
        spec_path = workdir / "pdf.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False))
        result = json.loads(await self._run_module(["jmshelf.pdf", str(spec_path)], cwd=workdir, timeout=3600))
        for f in cbz_files:
            Path(f).unlink(missing_ok=True)

        thumb = await self.ensure_cover(jm_id)
        volumes = result["volumes"]
        msg_ids, shas, total_size = [], [], 0
        for k, vol in enumerate(volumes, 1):
            path = Path(vol["path"])
            size = path.stat().st_size
            sha = await asyncio.to_thread(pack.file_sha256, path)
            label = f"卷 {k}/{len(volumes)}，" if len(volumes) > 1 else ""
            detail = (f"PDF · {len(chapters)} 章 · {vol['pages']} 页 · {size_text(size)}"
                      + (f" · 卷 {k}/{len(volumes)}" if len(volumes) > 1 else ""))
            msg = await self.storage.send_document(
                self.storage.fav_channel, path, self._caption(album["title"], jm_id, detail),
                path.name, thumb=thumb, buttons=self._read_button(f"a{jm_id}"),
                progress=self._progress(job, label))
            msg_ids.append(msg.id)
            shas.append(sha)
            total_size += size
            path.unlink(missing_ok=True)
        self.db.add_pdf_record(jm_id, msg_ids, total_size, ",".join(shas), job["user_id"])
        self.save_meta(jm_id)
        link = self.storage.post_link(self.storage.fav_channel_id, msg_ids[0])
        await self._status(job, f"完成，PDF {size_text(total_size)}", force=True,
                           buttons=[[Button.url("查看 PDF", link)]])

    # ---- 删除 ----
    async def remove_album(self, jm_id: int) -> tuple[int, int]:
        lib, pdf = self.db.remove_album(jm_id)
        await self.storage.delete(self.storage.channel, lib)
        await self.storage.delete(self.storage.fav_channel, pdf)
        for name in (f"{jm_id}.jpg", f"{jm_id}_thumb.jpg"):
            (self.cfg.cover_dir / name).unlink(missing_ok=True)
        self.save_meta(jm_id)
        return len(lib), len(pdf)

    # ---- JM 收藏夹同步 ----
    async def jm_sync(self, user_id: int, data: dict | None = None) -> tuple[dict, int | None]:
        """把 JM 收藏夹镜像成本地收藏夹，并为新收藏的作品安排入库和 PDF。"""
        if data is None:
            data = await self.jm.favorites(self.cfg.jm_username, self.cfg.jm_password)
        plan = sync.apply(self.db, user_id, data)
        for jm_id in plan["changed"]:
            self.save_meta(jm_id)
        batch = await self.start_batch(user_id, "JM 收藏夹同步", plan["archive"], plan["pdf"])
        self.db.set_meta("jmsync_at", str(int(time.time())))
        return plan, batch

    async def _sync_loop(self):
        while True:
            await asyncio.sleep(600)
            if self.db.get_meta("jmsync_auto") != "1" or not self.cfg.jm_username:
                continue
            if time.time() - int(self.db.get_meta("jmsync_at", "0")) < SYNC_INTERVAL:
                continue
            try:
                plan, _ = await self.jm_sync(self.cfg.owner_id)
                log.info("JM 收藏夹自动同步：%s", sync.summary_counts(plan))
            except Exception as e:
                log.exception("JM 收藏夹自动同步失败")
                self.db.set_meta("jmsync_at", str(int(time.time())))
                try:
                    await self.client.send_message(self.cfg.owner_id, f"JM 收藏夹自动同步失败：{clip(str(e), 200)}")
                except Exception:
                    pass

    # ---- 备份 ----
    async def _backup_loop(self):
        while True:
            last = int(self.db.get_meta("backup_at", "0"))
            wait = last + BACKUP_INTERVAL - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 3600))
                continue
            try:
                await self.backup()
            except Exception:
                log.exception("数据库备份失败")
                await asyncio.sleep(3600)

    async def backup(self):
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        snap = self.cfg.work_dir / "backup.db"
        gz = self.cfg.work_dir / f"jmshelf-{stamp}.db.gz"
        try:
            self.db.backup_to(snap)
            await asyncio.to_thread(self._gzip, snap, gz)
            msg = await self.storage.send_document(
                self.storage.channel, gz, f"#backup 数据库备份 {stamp}", gz.name, silent=True)
        finally:
            snap.unlink(missing_ok=True)
            gz.unlink(missing_ok=True)
        ids = json.loads(self.db.get_meta("backup_msgs", "[]")) + [msg.id]
        await self.storage.delete(self.storage.channel, ids[:-BACKUP_KEEP])
        self.db.set_meta("backup_msgs", json.dumps(ids[-BACKUP_KEEP:]))
        self.db.set_meta("backup_at", str(int(time.time())))
        log.info("数据库已备份到频道消息 %s", msg.id)

    @staticmethod
    def _gzip(src: Path, dest: Path):
        with open(src, "rb") as f, gzip.open(dest, "wb") as g:
            shutil.copyfileobj(f, g)
