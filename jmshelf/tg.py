"""频道存储：并行分块上传、按字节区间读取、并行整本下载。

实测（Bot 在 DC1）：Telethon 默认顺序上传只有 0.8MB/s，512KB 分块 4 路并发约 7MB/s；
按页读取拆成 128KB 块并行请求，单页中位数约 180ms。
"""
import asyncio
import os
import random
import time
from pathlib import Path

from telethon import TelegramClient, functions, types

UPLOAD_PART = 512 * 1024
UPLOAD_CONCURRENCY = 4
SMALL_FILE = 10 * 1024 * 1024
READ_CHUNK = 128 * 1024
DOWNLOAD_CHUNK = 512 * 1024
DOWNLOAD_CONCURRENCY = 6
MESSAGE_TTL = 30 * 60


class Storage:
    def __init__(self, client: TelegramClient, channel_id: int, fav_channel_id: int):
        self.client = client
        self.channel_id = channel_id
        self.fav_channel_id = fav_channel_id
        self.channel = None
        self.fav_channel = None
        self._messages: dict[tuple[int, int], tuple[float, object]] = {}
        self._read_sem = asyncio.Semaphore(16)

    async def init(self):
        self.channel = await self.client.get_entity(self.channel_id)
        self.fav_channel = await self.client.get_entity(self.fav_channel_id)

    @staticmethod
    def internal_id(channel_id: int) -> int:
        """t.me/c/<internal>/<msg> 链接用的频道 ID（去掉 -100 前缀）。"""
        return int(str(channel_id).removeprefix("-100"))

    def post_link(self, channel_id: int, msg_id: int) -> str:
        return f"https://t.me/c/{self.internal_id(channel_id)}/{msg_id}"

    async def upload(self, path: Path, progress=None):
        size = path.stat().st_size
        if size <= SMALL_FILE:
            return await self.client.upload_file(str(path), progress_callback=progress)
        file_id = random.getrandbits(63)
        total = -(-size // UPLOAD_PART)
        sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)
        done = 0
        fd = os.open(path, os.O_RDONLY)
        try:
            async def put(i):
                nonlocal done
                async with sem:
                    data = os.pread(fd, UPLOAD_PART, i * UPLOAD_PART)
                    for attempt in range(3):
                        try:
                            ok = await self.client(functions.upload.SaveBigFilePartRequest(file_id, i, total, data))
                            if ok:
                                break
                        except (ConnectionError, asyncio.TimeoutError):
                            if attempt == 2:
                                raise
                            await asyncio.sleep(1 + attempt)
                    else:
                        raise RuntimeError(f"分块 {i} 上传失败")
                done += len(data)
                if progress:
                    progress(done, size)

            await asyncio.gather(*(put(i) for i in range(total)))
        finally:
            os.close(fd)
        return types.InputFileBig(file_id, total, path.name)

    async def send_document(self, channel, path: Path, caption: str, file_name: str,
                            thumb: Path | None = None, buttons=None, progress=None, silent=False):
        handle = await self.upload(path, progress)
        return await self.client.send_file(
            channel, handle, caption=caption, parse_mode="html", force_document=True,
            attributes=[types.DocumentAttributeFilename(file_name)],
            thumb=str(thumb) if thumb and thumb.exists() else None,
            buttons=buttons, silent=silent,
        )

    async def message(self, channel, msg_id: int, fresh: bool = False):
        key = (channel.id, msg_id)
        hit = self._messages.get(key)
        if hit and not fresh and time.time() - hit[0] < MESSAGE_TTL:
            return hit[1]
        msg = await self.client.get_messages(channel, ids=msg_id)
        if msg is None or msg.media is None:
            raise LookupError(f"频道消息 {msg_id} 不存在或没有文件")
        self._messages[key] = (time.time(), msg)
        return msg

    async def _chunk(self, doc, offset: int) -> bytes:
        parts = [c async for c in self.client.iter_download(doc, offset=offset, request_size=READ_CHUNK, limit=1)]
        return b"".join(parts)

    async def read_range(self, msg_id: int, offset: int, size: int) -> bytes:
        """从书库频道的某条文件消息里读出 [offset, offset+size) 这一段。"""
        for attempt in range(2):
            msg = await self.message(self.channel, msg_id, fresh=attempt > 0)
            doc = msg.media.document
            start = offset - offset % READ_CHUNK
            count = -(-(offset + size - start) // READ_CHUNK)
            try:
                async with self._read_sem:
                    parts = await asyncio.gather(*(self._chunk(doc, start + i * READ_CHUNK) for i in range(count)))
                data = b"".join(parts)[offset - start: offset - start + size]
                if len(data) == size:
                    return data
            except Exception:
                if attempt == 1:
                    raise
        raise IOError(f"读取消息 {msg_id} 的区间失败")

    async def download(self, channel, msg_id: int, dest: Path, progress=None):
        msg = await self.message(channel, msg_id, fresh=True)
        doc = msg.media.document
        size = doc.size
        total = -(-size // DOWNLOAD_CHUNK)
        sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
        done = 0
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            async def get(i):
                nonlocal done
                async with sem:
                    parts = [c async for c in self.client.iter_download(
                        doc, offset=i * DOWNLOAD_CHUNK, request_size=DOWNLOAD_CHUNK, limit=1)]
                    data = b"".join(parts)
                    os.pwrite(fd, data, i * DOWNLOAD_CHUNK)
                done += len(data)
                if progress:
                    progress(done, size)

            await asyncio.gather(*(get(i) for i in range(total)))
        finally:
            os.close(fd)
        if dest.stat().st_size != size:
            raise IOError(f"下载不完整：{dest.stat().st_size}/{size}")

    async def delete(self, channel, msg_ids: list[int]):
        if msg_ids:
            await self.client.delete_messages(channel, msg_ids)
            for mid in msg_ids:
                self._messages.pop((channel.id, mid), None)
