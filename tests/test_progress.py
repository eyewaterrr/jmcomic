import asyncio

from jmshelf.db import DB
from jmshelf.jobs import Worker


def make_worker(tmp_path):
    db = DB(tmp_path / "t.db")
    return Worker(None, db, None, None, None, "bot"), db


def test_progress_for_queued_and_running(tmp_path):
    worker, db = make_worker(tmp_path)
    first, _ = db.add_job("archive", 20, 1, None)
    second, _ = db.add_job("archive", 10, 1, None)
    assert worker.progress_for("archive", 10) == {"percent": 0, "text": "排队中，前面还有 1 个任务", "queued": True}
    assert worker.progress_for("archive", 20)["text"] == "排队中"

    db.update_job(first, status="running")
    assert worker.progress_for("archive", 20) == {"percent": 0, "text": "准备中", "queued": False}
    worker._set_live(db.job(first), 0.4567, "下载 20/53 页")
    assert worker.progress_for("archive", 20) == {"percent": 46, "text": "下载 20/53 页", "queued": False}
    assert worker.progress_for("archive", 30) is None


def test_upload_progress_maps_into_chapter_span(tmp_path):
    worker, db = make_worker(tmp_path)
    job_id, _ = db.add_job("archive", 10, 1, None)
    job = db.job(job_id)
    worker._last_status[job_id] = float("inf")  # 不触发私聊进度消息
    callback = worker._progress(job, "第 2/4 章，", (0.25 + 0.25 * 0.88, 0.5))
    callback(50, 100)
    assert worker.live[job_id]["percent"] == 48 and "上传 50%" in worker.live[job_id]["text"]


def test_watch_download_counts_images(tmp_path):
    worker, db = make_worker(tmp_path)
    job_id, _ = db.add_job("archive", 10, 1, None)
    folder = tmp_path / "img"
    folder.mkdir()
    for i in range(5):
        (folder / f"{i:05d}.webp").write_bytes(b"x")

    async def run():
        task = asyncio.create_task(worker._watch_download(db.job(job_id), folder, 10, 0.0, 1.0, ""))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(run())
    assert worker.live[job_id] == {"percent": 42, "text": "下载 5/10 页"}
