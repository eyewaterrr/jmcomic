"""原图缓存：阅读时从 JM 按页下载原图（不解码），存在 <缓存目录>/pages/<章节ID>/0001.webp……

- 读者请求某页：缓存里有就直接给；没有就马上下载，排在所有预读前面，最多等 30 秒。
- 预读：每次请求后保证“当前页往后 N 页”在下载队列里。读者跳走后，窗口外还没开始的预读直接放弃。
- 上传精选时整章补齐（fill），排在读者请求之后、预读之前，不受窗口限制。
- 清理：超过 days 天没打开的章节删除；总大小超过上限时，按最近打开时间从旧到新删，降到上限的 90%。
  正在上传精选（加了锁）、正在下载、10 分钟内刚看过的章节不删。
- 每页尺寸在下载时从图片头里读出，交给 on_dims 回调（DimsRecorder 定时写进数据库，阅读页排版用）。

服务器只做网络传输和存文件，不解码图片：单核 CPU 不再是阅读的瓶颈。
"""
import asyncio
import io
import itertools
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

OPENED = ".opened"
WAIT_SECONDS = 30
ACTIVE_SECONDS = 600
PRIO_READER, PRIO_FILL, PRIO_AHEAD = 0, 1, 2


def _fetch(jm, url: str) -> tuple[bytes, tuple[int, int] | None]:
    data = jm.image_bytes(url)
    try:
        with Image.open(io.BytesIO(data)) as im:  # 只读图片头，不解码
            size = im.size
    except Exception:
        size = None
    return data, size


class PageCache:
    def __init__(self, root: Path, jm, max_bytes: int, days: int, ahead: int, threads: int, on_dims=None):
        self.root = root
        self.jm = jm
        self.max_bytes = max_bytes
        self.days = days
        self.ahead = ahead
        self.threads = threads
        self.on_dims = on_dims
        self.downloaded_bytes = 0          # 从 JM 下载的总字节数（/status 显示）
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="jmimg")
        self._queue: asyncio.PriorityQueue | None = None
        self._workers: list[asyncio.Task] = []
        self._seq = itertools.count()
        self._futures: dict[tuple[int, int], asyncio.Future] = {}
        self._info: dict[tuple[int, int], dict] = {}
        self._started: set[tuple[int, int]] = set()
        self._wanted: dict[int, tuple[int, float]] = {}   # 章节 → (读者最近请求的页, 时间)
        self._locks: dict[int, int] = {}

    # ---- 路径 ----
    def _dir(self, photo_id: int) -> Path:
        return self.root / str(photo_id)

    def path(self, photo_id: int, n: int, ext: str) -> Path:
        return self._dir(photo_id) / f"{n:04d}{ext}"

    def touch(self, photo_id: int):
        """记录这一章最近一次被打开的时间（清理按它判断）。"""
        d = self._dir(photo_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / OPENED).touch()

    def cached_pages(self, photo_id: int) -> int:
        d = self._dir(photo_id)
        if not d.is_dir():
            return 0
        return sum(1 for f in d.iterdir() if f.name[:4].isdigit() and not f.name.endswith(".tmp"))

    # ---- 下载 ----
    async def page(self, photo_id: int, n: int, chapter: dict) -> Path:
        """读者请求的页。chapter 是 jm.chapter() 的结果。超过 30 秒抛 TimeoutError。"""
        self._wanted[photo_id] = (n, time.time())
        info = chapter["pages"][n - 1]
        path = self.path(photo_id, n, info["ext"])
        if path.exists():
            return path
        fut = self._request(photo_id, n, info, PRIO_READER)
        return await asyncio.wait_for(asyncio.shield(fut), WAIT_SECONDS)

    def read_ahead(self, photo_id: int, n: int, chapter: dict):
        total = len(chapter["pages"])
        for m in range(n + 1, min(total, n + self.ahead) + 1):
            info = chapter["pages"][m - 1]
            if (photo_id, m) not in self._futures and not self.path(photo_id, m, info["ext"]).exists():
                self._request(photo_id, m, info, PRIO_AHEAD + (m - n))

    async def fill(self, photo_id: int, chapter: dict, progress=None) -> list[Path]:
        """整章补齐（上传精选用），返回每页的文件。progress(已有页数, 总页数)。"""
        pages = chapter["pages"]
        paths = [self.path(photo_id, i, p["ext"]) for i, p in enumerate(pages, 1)]
        missing = [i for i, p in enumerate(paths, 1) if not p.exists()]
        done = len(pages) - len(missing)
        if progress:
            progress(done, len(pages))
        futs = [self._request(photo_id, i, pages[i - 1], PRIO_FILL) for i in missing]
        for fut in asyncio.as_completed(futs):
            await fut
            done += 1
            if progress:
                progress(done, len(pages))
        return paths

    def _request(self, photo_id: int, n: int, info: dict, priority: int) -> asyncio.Future:
        if self._queue is None:
            self._queue = asyncio.PriorityQueue()
            loop = asyncio.get_running_loop()
            self._workers = [loop.create_task(self._worker()) for _ in range(self.threads)]
        key = (photo_id, n)
        fut = self._futures.get(key)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            fut.add_done_callback(lambda f: f.cancelled() or f.exception())  # 没人等的失败不报警告
            self._futures[key] = fut
            self._info[key] = info
        if key not in self._started:
            # 同一页可能排进队列多次（例如先是预读、后来读者直接要），按最高优先级的那次执行，其余跳过
            self._queue.put_nowait((priority, next(self._seq), key))
        return fut

    def _in_window(self, photo_id: int, n: int) -> bool:
        wanted = self._wanted.get(photo_id)
        return wanted is not None and wanted[0] <= n <= wanted[0] + self.ahead + 5

    async def _worker(self):
        while True:
            priority, _, key = await self._queue.get()
            fut = self._futures.get(key)
            if fut is None or fut.done() or key in self._started:
                continue
            photo_id, n = key
            if priority >= PRIO_AHEAD and not self._in_window(photo_id, n):
                self._forget(key)
                fut.cancel()
                continue
            self._started.add(key)
            try:
                fut.set_result(await self._download(photo_id, n, self._info[key]))
            except asyncio.CancelledError:
                fut.cancel()
                raise
            except Exception as e:
                fut.set_exception(e)
            finally:
                self._forget(key)

    def _forget(self, key):
        self._started.discard(key)
        self._futures.pop(key, None)
        self._info.pop(key, None)

    async def _download(self, photo_id: int, n: int, info: dict) -> Path:
        path = self.path(photo_id, n, info["ext"])
        if path.exists():
            return path
        data, size = await asyncio.get_running_loop().run_in_executor(self._pool, _fetch, self.jm, info["url"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self.downloaded_bytes += len(data)
        if self.on_dims and size:
            self.on_dims(photo_id, n, *size)
        return path

    # ---- 锁和清理 ----
    @contextmanager
    def lock(self, photo_ids):
        """上传精选期间锁住这些章节，清理时跳过。"""
        photo_ids = list(photo_ids)
        for pid in photo_ids:
            self._locks[pid] = self._locks.get(pid, 0) + 1
        try:
            yield
        finally:
            for pid in photo_ids:
                self._locks[pid] -= 1
                if not self._locks[pid]:
                    del self._locks[pid]

    def _busy(self, photo_id: int, now: float) -> bool:
        if self._locks.get(photo_id):
            return True
        wanted = self._wanted.get(photo_id)
        if wanted and now - wanted[1] < ACTIVE_SECONDS:
            return True
        return any(k[0] == photo_id for k in list(self._futures))

    def scan(self) -> list[tuple[float, int, Path, int]]:
        """[(最近打开时间, 章节ID, 目录, 字节数), ...]"""
        out = []
        if not self.root.is_dir():
            return out
        for d in self.root.iterdir():
            if not d.is_dir() or not d.name.isdigit():
                continue
            marker = d / OPENED
            try:
                opened = marker.stat().st_mtime if marker.exists() else d.stat().st_mtime
                size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
            except FileNotFoundError:
                continue
            out.append((opened, int(d.name), d, size))
        return out

    def usage(self) -> tuple[int, int]:
        """(总字节数, 章节数)"""
        chapters = self.scan()
        return sum(c[3] for c in chapters), len(chapters)

    def cleanup(self, now: float | None = None) -> dict:
        now = now or time.time()
        chapters = sorted(self.scan())
        total = sum(c[3] for c in chapters)
        expired = evicted = 0
        kept = []
        for opened, pid, d, size in chapters:
            if now - opened > self.days * 86400 and not self._busy(pid, now):
                shutil.rmtree(d, ignore_errors=True)
                total -= size
                expired += 1
            else:
                kept.append((opened, pid, d, size))
        if total > self.max_bytes:
            target = int(self.max_bytes * 0.9)
            for opened, pid, d, size in kept:
                if total <= target:
                    break
                if self._busy(pid, now):
                    continue
                shutil.rmtree(d, ignore_errors=True)
                total -= size
                evicted += 1
        if expired or evicted:
            log.info("缓存清理：过期 %s 章，超出上限删 %s 章，现在 %.0fMB", expired, evicted, total / 1048576)
        return {"expired": expired, "evicted": evicted, "total": total}

    async def cleanup_loop(self, interval: int = 3600):
        while True:
            try:
                await asyncio.to_thread(self.cleanup)
            except Exception:
                log.exception("缓存清理失败")
            await asyncio.sleep(interval)


class DimsRecorder:
    """收集下载时读到的每页尺寸，定时合并进 chapters.dims。"""

    def __init__(self, db):
        self.db = db
        self.pending: dict[int, dict[int, list[int]]] = {}

    def __call__(self, photo_id: int, n: int, width: int, height: int):
        self.pending.setdefault(photo_id, {})[n] = [width, height]

    def flush(self):
        pending, self.pending = self.pending, {}
        for photo_id, pages in pending.items():
            row = self.db.chapter(photo_id)
            if row is None:
                continue
            dims = self.db.chapter_dims(photo_id)
            total = max(row["page_count"], max(pages), len(dims))
            dims += [None] * (total - len(dims))
            for n, size in pages.items():
                dims[n - 1] = size
            self.db.set_chapter_dims(photo_id, dims)

    async def loop(self, interval: int = 5):
        while True:
            await asyncio.sleep(interval)
            try:
                self.flush()
            except Exception:
                log.exception("写入页面尺寸失败")
