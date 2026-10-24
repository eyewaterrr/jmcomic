import asyncio
import io
import json
import sys
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image

from jmshelf import pack, remote
from jmshelf.jobs import Worker

REAL_WARM = remote.warm  # 下面的 autouse 夹具会把它换成空函数

ALBUM = {"jm_id": 9, "title": "测试", "authors": [], "works": [], "tags": [],
         "page_count": 8, "chapters": [{"photo_id": 9, "sort": 1, "title": "第1章"}]}


def write_images(folder, count, start=0):
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(start, start + count):
        Image.new("RGB", (40 + i, 60), (i * 30 % 255, 90, 120)).save(folder / f"{i + 1:05d}.webp", quality=80)


def test_parse_args_validates_everything():
    assert remote.parse_args(["123", "16", "2.7.10"]) == ("123", 16, "2.7.10")
    assert remote.parse_args(["1", "500", "2.7"])[1] == remote.MAX_THREADS
    for bad in (["1", "4"], ["1;id", "4", "2.7.10"], ["1", "4", "2.7.10;id"], ["1", "-4", "2.7.10"],
                ["1", "4", "2.7.10", "x"], ["check"]):
        with pytest.raises(ValueError):
            remote.parse_args(bad)


def test_serve_writes_only_the_cbz_to_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "_ensure_jmcomic", lambda version: None)
    monkeypatch.setattr(remote, "_download", lambda photo_id, img_dir, threads: write_images(img_dir, 3))
    out = io.BytesIO()
    spec = io.StringIO(json.dumps({"album": ALBUM, "chapter": ALBUM["chapters"][0]}))
    remote.serve(["9", "16", "2.7.10"], spec, out)
    cbz = tmp_path / "got.cbz"
    cbz.write_bytes(out.getvalue())
    pages, sha = pack.cbz_index(cbz)
    assert [p.name for p in pages] == ["0001.webp", "0002.webp", "0003.webp"] and len(sha) == 64
    assert [p.width for p in pages] == [40, 41, 42]
    with zipfile.ZipFile(cbz) as zf:
        assert "<PageCount>3</PageCount>" in zf.read(pack.COMICINFO).decode()


def ssh_cfg(tmp_path):
    return SimpleNamespace(remote_host="u@h", remote_port=3856, remote_key="/k", remote_known_hosts="/kh",
                           remote_threads=16, remote_socket=tmp_path / "remote.sock")


def test_ssh_command_pins_host_key_and_reuses_connection(tmp_path):
    cmd = remote.ssh_command(ssh_cfg(tmp_path), 9)
    assert cmd[:3] == ["ssh", "-F", "none"] and "StrictHostKeyChecking=yes" in cmd and "UserKnownHostsFile=/kh" in cmd
    assert f"ControlPath={tmp_path / 'remote.sock'}" in cmd and "ControlMaster=no" in cmd
    assert cmd[-4:-1] == ["u@h", "9", "16"] and remote.VERSION_RE.fullmatch(cmd[-1])


def test_warm_opens_master_only_when_missing(tmp_path, monkeypatch):
    calls, alive = [], {"value": False}

    class Proc:
        def __init__(self, code):
            self.code = code

        async def wait(self):
            return self.code

    async def fake_exec(*cmd, **kwargs):
        calls.append(cmd)
        if "-O" in cmd:
            return Proc(0 if alive["value"] else 255)
        alive["value"] = True
        return Proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = ssh_cfg(tmp_path)
    cfg.remote_socket.write_text("stale")
    asyncio.run(REAL_WARM(cfg))
    asyncio.run(REAL_WARM(cfg))
    assert len(calls) == 3  # 检查、建主连接、再检查（已存在就不再建）
    assert "-N" in calls[1] and "-f" in calls[1] and "ControlMaster=yes" in calls[1]
    assert not cfg.remote_socket.exists()  # 失效的 socket 先删掉


def test_cbz_index_rejects_truncated_file(tmp_path):
    write_images(tmp_path / "img", 3)
    cbz = tmp_path / "a.cbz"
    pack.write_cbz(pack.list_images(tmp_path / "img"), cbz, "<ComicInfo/>")
    cbz.write_bytes(cbz.read_bytes()[:-200])
    with pytest.raises(zipfile.BadZipFile):
        pack.cbz_index(cbz)


# ---- 入库时本机 / 远程的分流 ----
@pytest.fixture(autouse=True)
def no_real_ssh(monkeypatch):
    async def warm(cfg):
        pass
    monkeypatch.setattr(remote, "warm", warm)


def make_worker(tmp_path, remote_host="u@h", min_mb=0):
    cfg = SimpleNamespace(image_threads=4, remote_host=remote_host, remote_min_mb=min_mb, data_dir=tmp_path)
    worker = Worker(cfg, None, None, None, None, "bot")

    async def status(*args, **kwargs):
        pass

    worker._status = status
    return worker


def fake_local(calls, pages, hang_after=None):
    """模拟本机下载子进程：写出图片；给了 hang_after 就在写够这么多页后一直挂着（等被取消）。"""
    async def run_module(args, cwd, timeout):
        img_dir = cwd / args[2].rsplit("/", 1)[-1]
        calls.append("local")
        write_images(img_dir, hang_after or pages)
        if hang_after:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                calls.append("cancelled")
                raise
        return "{}"
    return run_module


def run_chapter(worker, tmp_path):
    job = {"id": 1}
    return asyncio.run(worker._chapter_cbz(job, ALBUM, ALBUM["chapters"][0], tmp_path, 8, 0.0, 1.0, ""))


def test_small_chapter_stays_local(tmp_path):
    worker, calls = make_worker(tmp_path, min_mb=30), []
    worker._run_module = fake_local(calls, 12)
    cbz, pages, _ = run_chapter(worker, tmp_path)
    assert calls == ["local"] and len(pages) == 12 and cbz.exists()
    assert not (tmp_path / "9").exists()


def test_large_chapter_moves_to_remote(tmp_path, monkeypatch):
    worker, calls = make_worker(tmp_path, min_mb=0), []
    worker._run_module = fake_local(calls, 12, hang_after=9)

    async def fetch_cbz(cfg, album, chapter, out, timeout, on_progress=None):
        calls.append("remote")
        write_images(tmp_path / "remote-img", 12)
        pack.write_cbz(pack.list_images(tmp_path / "remote-img"), out, "<ComicInfo/>")

    monkeypatch.setattr(remote, "fetch_cbz", fetch_cbz)
    cbz, pages, _ = run_chapter(worker, tmp_path)
    assert calls == ["local", "cancelled", "remote"] and len(pages) == 12
    assert not (tmp_path / "9").exists()


def test_remote_failure_falls_back_to_local(tmp_path, monkeypatch):
    worker, calls = make_worker(tmp_path, min_mb=0), []
    first = fake_local(calls, 12, hang_after=9)
    second = fake_local(calls, 12)
    worker._run_module = lambda *a, **k: (first if calls.count("local") == 0 else second)(*a, **k)

    async def fetch_cbz(cfg, album, chapter, out, timeout, on_progress=None):
        calls.append("remote")
        out.write_bytes(b"partial")
        raise RuntimeError("连接断开")

    monkeypatch.setattr(remote, "fetch_cbz", fetch_cbz)
    cbz, pages, _ = run_chapter(worker, tmp_path)
    assert calls == ["local", "cancelled", "remote", "local"] and len(pages) == 12
    assert cbz.exists() and cbz.stat().st_size > 1000


def test_no_remote_configured_never_probes(tmp_path):
    worker, calls = make_worker(tmp_path, remote_host="", min_mb=0), []
    worker._run_module = fake_local(calls, 12)
    _, pages, _ = run_chapter(worker, tmp_path)
    assert calls == ["local"] and len(pages) == 12


def test_estimate_ignores_small_cover_pages(tmp_path):
    folder = tmp_path / "img"
    folder.mkdir()
    for i, kb in enumerate([5, 5, 40, 40, 40, 40, 40, 40, 0, 0, 0, 0]):  # 0 字节：还在编码
        (folder / f"{i + 1:05d}.webp").write_bytes(b"x" * kb * 1024)

    async def run():
        fetch = asyncio.create_task(asyncio.sleep(10))
        try:
            return await Worker._estimate_bytes(fetch, folder, 100)
        finally:
            fetch.cancel()

    assert asyncio.run(run()) == 40 * 1024 * 100


def test_fetch_cbz_reports_progress_and_keeps_stdout_clean(tmp_path, monkeypatch):
    script = ("import sys\n"
              "for n in (3, 7): print(f'progress {n}', file=sys.stderr, flush=True)\n"
              "print('JM 章节 9：12 页', file=sys.stderr)\n"
              "sys.stdout.buffer.write(b'CBZ-BYTES' + sys.stdin.buffer.read()[:2])\n")
    monkeypatch.setattr(remote, "ssh_command", lambda cfg, photo_id: [sys.executable, "-c", script])
    seen, out = [], tmp_path / "x.cbz"
    asyncio.run(remote.fetch_cbz(None, ALBUM, ALBUM["chapters"][0], out, timeout=30, on_progress=seen.append))
    assert seen == [3, 7] and out.read_bytes() == b'CBZ-BYTES{"'


def test_fetch_cbz_error_includes_last_log_lines(tmp_path, monkeypatch):
    script = "import sys\nprint('progress 1', file=sys.stderr)\nprint('下载失败：403', file=sys.stderr)\nsys.exit(1)\n"
    monkeypatch.setattr(remote, "ssh_command", lambda cfg, photo_id: [sys.executable, "-c", script])
    with pytest.raises(RuntimeError, match="下载失败：403"):
        asyncio.run(remote.fetch_cbz(None, ALBUM, ALBUM["chapters"][0], tmp_path / "x.cbz", timeout=30))
