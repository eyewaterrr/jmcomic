import asyncio
import io
import os
import threading
import time

from PIL import Image

from jmshelf.cache import OPENED, DimsRecorder, PageCache
from jmshelf.db import DB


def png(width=8, height=12) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height)).save(buf, "PNG")
    return buf.getvalue()


class FakeJM:
    """记录下载顺序；gate 不为空时，下载 u1 会卡住直到放行。"""

    def __init__(self, gate=None, fail=()):
        self.calls = []
        self.gate = gate
        self.fail = set(fail)
        self._lock = threading.Lock()

    def image_bytes(self, url):
        with self._lock:
            self.calls.append(url)
        if self.gate is not None and url == "u1":
            self.gate.wait(5)
        if url in self.fail:
            raise IOError("JM 取不到")
        return png()


def chapter(n):
    return {"pages": [{"url": f"u{i}", "num": 0, "ext": ".png"} for i in range(1, n + 1)]}


def make(tmp_path, jm, threads=2, ahead=5, max_bytes=10**9, days=7, on_dims=None):
    return PageCache(tmp_path / "pages", jm, max_bytes, days, ahead, threads, on_dims=on_dims)


def test_page_downloads_once_and_reports_size(tmp_path):
    jm = FakeJM()
    seen = []
    cache = make(tmp_path, jm, on_dims=lambda *a: seen.append(a))

    async def run():
        first = await cache.page(7, 1, chapter(3))
        again = await cache.page(7, 1, chapter(3))
        return first, again

    first, again = asyncio.run(run())
    assert first == again and first.read_bytes() == png()
    assert jm.calls == ["u1"] and seen == [(7, 1, 8, 12)]
    assert cache.cached_pages(7) == 1 and cache.downloaded_bytes == len(png())


def test_reader_request_jumps_the_queue_and_stale_read_ahead_is_dropped(tmp_path):
    gate = threading.Event()
    jm = FakeJM(gate)
    cache = make(tmp_path, jm, threads=1, ahead=10)
    ch = chapter(40)

    async def run():
        first = asyncio.create_task(cache.page(1, 1, ch))
        await asyncio.sleep(0.05)          # u1 正在下载（卡住），唯一的下载位被占着
        cache.read_ahead(1, 1, ch)         # 排进 2–11 的预读
        jump = asyncio.create_task(cache.page(1, 20, ch))   # 读者直接跳到第 20 页
        await asyncio.sleep(0.05)
        gate.set()
        await first
        await jump
        cache.read_ahead(1, 20, ch)
        await asyncio.sleep(0.3)

    asyncio.run(run())
    assert jm.calls[:2] == ["u1", "u20"]
    assert not {"u2", "u3", "u11"} & set(jm.calls)      # 跳走后，窗口外的预读放弃
    assert {"u21", "u25", "u30"} <= set(jm.calls)        # 新位置往后预读


def test_fill_downloads_everything_and_reports_progress(tmp_path):
    jm = FakeJM()
    cache = make(tmp_path, jm, ahead=2)
    ch = chapter(12)
    progress = []

    async def run():
        await cache.page(3, 1, ch)
        return await cache.fill(3, ch, lambda done, total: progress.append((done, total)))

    paths = asyncio.run(run())
    assert len(paths) == 12 and all(p.exists() for p in paths)
    assert progress[0] == (1, 12) and progress[-1] == (12, 12)
    assert sorted(jm.calls, key=lambda u: int(u[1:])) == [f"u{i}" for i in range(1, 13)]


def test_failed_download_raises_and_can_retry(tmp_path):
    jm = FakeJM(fail={"u2"})
    cache = make(tmp_path, jm)

    async def run():
        try:
            await cache.page(5, 2, chapter(3))
        except IOError:
            pass
        else:
            raise AssertionError("应该报错")
        jm.fail.clear()
        return await cache.page(5, 2, chapter(3))

    assert asyncio.run(run()).exists()


def fake_chapter_dir(root, photo_id, size, age_days):
    d = root / str(photo_id)
    d.mkdir(parents=True)
    (d / "0001.webp").write_bytes(b"x" * size)
    (d / OPENED).touch()
    t = time.time() - age_days * 86400
    os.utime(d / OPENED, (t, t))


def test_cleanup_expires_old_and_evicts_least_recent(tmp_path):
    cache = make(tmp_path, FakeJM(), max_bytes=1000, days=7)
    root = tmp_path / "pages"
    fake_chapter_dir(root, 1, 100, age_days=10)    # 过期
    fake_chapter_dir(root, 2, 100, age_days=9)     # 过期但正在上传精选（加锁）
    fake_chapter_dir(root, 3, 500, age_days=3)     # 超出上限时最先删
    fake_chapter_dir(root, 4, 400, age_days=2)
    fake_chapter_dir(root, 5, 300, age_days=1)
    with cache.lock([2]):
        result = cache.cleanup()
    assert result["expired"] == 1 and result["evicted"] == 1
    assert sorted(int(d.name) for d in root.iterdir()) == [2, 4, 5]
    assert cache.usage() == (800, 3)


def test_cleanup_keeps_chapter_being_read(tmp_path):
    cache = make(tmp_path, FakeJM(), max_bytes=10, days=7)
    fake_chapter_dir(tmp_path / "pages", 9, 100, age_days=30)
    cache._wanted[9] = (5, time.time())
    assert cache.cleanup()["expired"] == 0


def test_dims_recorder_merges_into_db(tmp_path):
    db = DB(tmp_path / "t.db")
    db.upsert_album({"jm_id": 1, "title": "t", "authors": [], "works": [], "tags": [],
                     "chapters": [{"photo_id": 1, "sort": 1, "title": ""}]})
    db.set_chapter_pages(1, 4)
    rec = DimsRecorder(db)
    rec(1, 3, 800, 1200)
    rec(1, 1, 700, 1000)
    rec(99, 1, 1, 1)                                   # 数据库里没有的章节忽略
    rec.flush()
    assert db.chapter_dims(1) == [[700, 1000], None, [800, 1200], None]
    rec(1, 2, 600, 900)
    rec.flush()
    assert db.chapter_dims(1)[1] == [600, 900] and rec.pending == {}
