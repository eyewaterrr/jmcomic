"""精选频道：并行分块上传 PDF、删除帖子。

v3 不再读写书库频道，Telegram 只用来存精选 PDF。
实测（Bot 在 DC1）：Telethon 默认顺序上传只有 0.8MB/s，512KB 分块 4 路并发约 7MB/s。
"""
import asyncio
import os
import random
from pathlib import Path

from telethon import TelegramClient, functions, types

UPLOAD_PART = 512 * 1024
UPLOAD_CONCURRENCY = 4
SMALL_FILE = 10 * 1024 * 1024


class Storage:
    def __init__(self, client: TelegramClient, fav_channel_id: int):
        self.client = client
        self.fav_channel_id = fav_channel_id
        self.fav_channel = None

    async def init(self):
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

    async def delete(self, channel, msg_ids: list[int]):
        if msg_ids:
            await self.client.delete_messages(channel, msg_ids)
