"""任务队列（串行执行）：

- archive：取详情 → 子进程下载每章 → 打包 CBZ → 并行上传书库频道 → 写索引
- favorite：从书库频道取回各章 → 子进程转 PDF → 上传精选频道
- 每 24 小时把数据库快照发到书库频道，只保留最近 3 份
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

from . import pack, ui
from .fmt import clip, esc, size_text

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MIN_FREE_BYTES = 1536 * 1024 * 1024
STATUS_INTERVAL = 3
BACKUP_INTERVAL = 24 * 3600
BACKUP_KEEP = 3
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class JobError(Exception):
    """不需要重试的错误，原样告诉用户。"""


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

    # ---- 提交 ----
    async def submit(self, kind: str, jm_id: int, user_id: int) -> tuple[str, int]:
        """建任务并在私聊里发一条进度消息。返回 (状态, 任务 ID)，状态为 created / exists。"""
        active = self.db.active_job(kind, jm_id)
        if active:
            return "exists", active["id"]
        msg = await self.client.send_message(user_id, f"JM{jm_id} {ui.KIND_NAME[kind]}：已加入队列")
        job_id, _ = self.db.add_job(kind, jm_id, user_id, user_id, msg.id)
        self._wake.set()
        return "created", job_id

    # ---- 主循环 ----
    async def run(self):
        self.db.requeue_running()
        for d in (self.cfg.work_dir, self.cfg.cover_dir, self.cfg.cache_dir):
            d.mkdir(parents=True, exist_ok=True)
        backup_task = asyncio.create_task(self._backup_loop())
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
            backup_task.cancel()

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
        try:
            if row["msg_id"]:
                await self.client.edit_message(row["chat_id"], row["msg_id"], body, parse_mode="html",
                                               buttons=buttons, link_preview=False)
            else:
                msg = await self.client.send_message(row["chat_id"], body, parse_mode="html",
                                                     buttons=buttons, link_preview=False)
                self.db.update_job(job["id"], msg_id=msg.id)
        except MessageNotModifiedError:
            pass
        except Exception:
            log.warning("更新任务 %s 进度消息失败", job["id"], exc_info=True)

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

    # ---- 封面 ----
    async def ensure_cover(self, jm_id: int) -> Path | None:
        web = self.cfg.cover_dir / f"{jm_id}.jpg"
        thumb = self.cfg.cover_dir / f"{jm_id}_thumb.jpg"
        if web.exists() and thumb.exists():
            return thumb
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
    def _caption(self, title: str, jm_id: int, detail: str, meta: str) -> str:
        return (f"<b>[JM{jm_id}] {esc(clip(title, 300))}</b>\n{esc(detail)}\n"
                f"#JM{jm_id}\n<code>meta: {meta} v=1</code>")

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
            await self._run_module(
                ["jmshelf.fetch", str(ch["photo_id"]), str(img_dir), str(self.cfg.image_threads)],
                cwd=workdir, timeout=3600)
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
            meta = (f"jm={jm_id} photo={ch['photo_id']} ep={i}/{total} sort={ch['sort']} "
                    f"pages={len(pages)} sha256={sha}")
            msg = await self.storage.send_document(
                self.storage.channel, cbz, self._caption(album["title"], jm_id, detail, meta),
                _file_name(name), thumb=thumb, buttons=self._read_button(f"c{ch['photo_id']}"),
                progress=self._progress(job, label))
            self.db.add_chapter(jm_id, ch["photo_id"], ch["sort"], ch["title"], msg.id, size, sha, pages)
            cbz.unlink(missing_ok=True)

        # 先标记完成，按钮才会显示“收藏”而不是“入库中”
        self.db.update_job(job["id"], status="done")
        _, buttons = ui.album_view(self.cfg, self.db, self.storage, album)
        pages = sum(r["page_count"] for r in self.db.chapters(jm_id))
        await self._status(job, f"完成，{total} 章 {pages} 页", force=True, buttons=buttons)

    # ---- 收藏 ----
    async def _favorite(self, job, workdir: Path):
        jm_id = job["jm_id"]
        album = self.db.album(jm_id)
        chapters = self.db.chapters(jm_id)
        if not album or len(chapters) < album["chapter_count"]:
            raise JobError("这本还没有完整入库，请先入库")
        if self.db.favorite(jm_id):
            await self._status(job, "已经收藏过了", force=True)
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
            meta = f"jm={jm_id} kind=pdf vol={k}/{len(volumes)} pages={vol['pages']} sha256={sha}"
            msg = await self.storage.send_document(
                self.storage.fav_channel, path, self._caption(album["title"], jm_id, detail, meta),
                path.name, thumb=thumb, buttons=self._read_button(f"a{jm_id}"),
                progress=self._progress(job, label))
            msg_ids.append(msg.id)
            shas.append(sha)
            total_size += size
            path.unlink(missing_ok=True)
        self.db.add_favorite(jm_id, msg_ids, total_size, ",".join(shas), job["user_id"])
        link = self.storage.post_link(self.storage.fav_channel_id, msg_ids[0])
        await self._status(job, f"完成，PDF {size_text(total_size)}", force=True,
                           buttons=[[Button.url("查看收藏", link)]])

    # ---- 删除 ----
    async def remove_album(self, jm_id: int) -> tuple[int, int]:
        lib, fav = self.db.remove_album(jm_id)
        await self.storage.delete(self.storage.channel, lib)
        await self.storage.delete(self.storage.fav_channel, fav)
        for name in (f"{jm_id}.jpg", f"{jm_id}_thumb.jpg"):
            (self.cfg.cover_dir / name).unlink(missing_ok=True)
        return len(lib), len(fav)

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
