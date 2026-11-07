"""远程下载机：大章节交给多核服务器下载、解码、打包，CBZ 经 SSH 传回本机。

本机只有 1 核，长篇下载解码是瓶颈。入库时先在本机下载，前几页显示这一章超过 JM_REMOTE_MIN_MB
时改由远程下载机处理（见 jobs.py）。一章只走一次 SSH：

- 请求：命令行 "<photo_id> <线程数> <jmcomic 版本>"，标准输入是 {"album": ..., "chapter": ...}（几 KB）
- 应答：标准输出只有 CBZ 文件本身；日志和错误写到标准错误，退出码非 0 表示失败
- 进度：下载期间每秒往标准错误写一行 "progress <已下载页数>"，本机据此显示进度条
- 命令行 "check" 只回报版本，用来检查连通

每页偏移和 SHA-256 由本机从收到的 CBZ 计算，不另外传。远程下载机的 authorized_keys 用强制命令
限定这把密钥只能运行 deploy/remote-run.sh，安装见 deploy/remote-install.sh。
"""
import asyncio
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from . import pack

MAX_THREADS = 64
VERSION_RE = re.compile(r"\d+(\.\d+){1,3}")


# ---- 本机：发起请求 ----
def _ssh_base(cfg) -> list[str]:
    return [
        "ssh", "-F", "none", "-p", str(cfg.remote_port), "-i", str(cfg.remote_key),
        "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={cfg.remote_known_hosts}",
        "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
        "-o", f"ControlPath={cfg.remote_socket}",
    ]


def ssh_command(cfg, photo_id: int) -> list[str]:
    # 有 warm() 建好的主连接就复用，没有就直接连
    version = importlib.metadata.version("jmcomic")
    return [*_ssh_base(cfg), "-o", "ControlMaster=no",
            cfg.remote_host, str(photo_id), str(cfg.remote_threads), version]


async def warm(cfg):
    """预先建好 SSH 主连接，空闲 10 分钟后自动断开。

    本机试下前几页时调用，交给远程时就不用再握手：新建连接约 2.2 秒，复用只要约 0.5 秒。
    主连接不开会话（-N），所以不会触发远程的强制命令。
    """
    base = _ssh_base(cfg)
    devnull = asyncio.subprocess.DEVNULL
    check = await asyncio.create_subprocess_exec(*base, "-O", "check", cfg.remote_host,
                                                 stdout=devnull, stderr=devnull)
    if await check.wait() == 0:
        return
    Path(cfg.remote_socket).unlink(missing_ok=True)  # 服务重启后留下的失效 socket
    # 主连接会转入后台长期运行，标准输入输出接到 /dev/null，不占用调用方的管道
    proc = await asyncio.create_subprocess_exec(
        *base, "-o", "ControlMaster=yes", "-o", "ControlPersist=600", "-N", "-f", cfg.remote_host,
        stdin=devnull, stdout=devnull, stderr=devnull)
    await proc.wait()


async def _kill(proc):
    if proc.returncode is None:
        proc.kill()
    await proc.wait()


async def fetch_cbz(cfg, album: dict, chapter: dict, out: Path, timeout: int, on_progress=None):
    """在远程下载机上处理一章，CBZ 从 SSH 的标准输出直接写进 out。on_progress(已下载页数) 每秒左右调用一次。"""
    spec = json.dumps({"album": album, "chapter": chapter}, ensure_ascii=False).encode()
    tail = deque(maxlen=3)  # 最后几行日志，出错时放进异常信息

    async def run(proc):
        proc.stdin.write(spec)
        await proc.stdin.drain()
        proc.stdin.close()
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line.startswith("progress "):
                if on_progress:
                    on_progress(int(line.split()[1]))
            elif line:
                tail.append(line)
        await proc.wait()

    with open(out, "wb") as f:
        proc = await asyncio.create_subprocess_exec(
            *ssh_command(cfg, chapter["photo_id"]),
            stdin=asyncio.subprocess.PIPE, stdout=f, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(run(proc), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill(proc)
            raise RuntimeError("远程下载超时")
        except BaseException:  # 取消，或远程提前断开导致写标准输入失败
            await _kill(proc)
            raise
    if proc.returncode != 0:
        raise RuntimeError(f"远程下载出错（退出码 {proc.returncode}）：{' | '.join(tail)}")


# ---- 远程下载机：处理请求 ----
def parse_args(args: list[str]) -> tuple[str, int, str]:
    if (len(args) != 3 or not args[0].isdigit() or not args[1].isdigit()
            or not VERSION_RE.fullmatch(args[2])):
        raise ValueError("参数应为：<photo_id> <线程数> <jmcomic 版本>")
    return args[0], max(1, min(int(args[1]), MAX_THREADS)), args[2]


def _ensure_jmcomic(version: str):
    """和本机用同一个 jmcomic 版本；本机自动升级后，下一次请求时这里跟着装。"""
    try:
        if importlib.metadata.version("jmcomic") == version:
            return
    except importlib.metadata.PackageNotFoundError:
        pass
    print(f"安装 jmcomic {version}", file=sys.stderr)
    subprocess.run(["uv", "pip", "install", "-q", "-p", sys.executable, f"jmcomic=={version}"],
                   stdout=sys.stderr, check=True)


def _download(photo_id: str, img_dir: Path, threads: int):
    # 子进程的标准输出转到标准错误，标准输出只留给 CBZ
    proc = subprocess.Popen([sys.executable, "-m", "jmshelf.fetch", photo_id, str(img_dir), str(threads)],
                            stdout=sys.stderr)
    while proc.poll() is None:
        done = len(pack.list_images(img_dir)) if img_dir.exists() else 0
        print(f"progress {done}", file=sys.stderr, flush=True)
        time.sleep(1)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, proc.args)


def serve(args: list[str], stdin, stdout):
    photo_id, threads, version = parse_args(args)
    _ensure_jmcomic(version)
    spec = json.load(stdin)
    with tempfile.TemporaryDirectory(prefix="jmshelf-") as tmp:
        img_dir = Path(tmp) / "img"
        _download(photo_id, img_dir, threads)
        images = pack.list_images(img_dir)
        if not images:
            raise RuntimeError("没有下载到图片")
        cbz = Path(tmp) / "out.cbz"
        pack.write_cbz(images, cbz, pack.comicinfo_xml(spec["album"], spec["chapter"], len(images)))
        with open(cbz, "rb") as f:
            shutil.copyfileobj(f, stdout)
        print(f"JM 章节 {photo_id}：{len(images)} 页，{cbz.stat().st_size / 1e6:.1f}MB", file=sys.stderr)


def main() -> int:
    args = os.environ.get("SSH_ORIGINAL_COMMAND", " ".join(sys.argv[1:])).split()
    if args == ["check"]:  # 安装后检查连通：只回报版本
        print(f"ok jmcomic {importlib.metadata.version('jmcomic')} python {sys.version.split()[0]}")
        return 0
    try:
        serve(args, sys.stdin, sys.stdout.buffer)
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
