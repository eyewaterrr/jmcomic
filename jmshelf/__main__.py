"""启动服务：python -m jmshelf

运维用的子命令（服务运行时也能用，任务由服务里的队列执行）：
    python -m jmshelf enqueue archive|favorite <JM号>
"""
import asyncio
import logging
import sys

import uvicorn
from telethon import TelegramClient

from . import config
from .bot import Bot
from .db import DB
from .jm import JM
from .jobs import Worker
from .tg import Storage
from .web import PageCache, Reader, create_app

log = logging.getLogger("jmshelf")


async def serve(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = DB(cfg.db_path)
    db.ensure_owner(cfg.owner_id)

    client = TelegramClient(str(cfg.session_path), cfg.api_id, cfg.api_hash,
                            use_ipv6=False, flood_sleep_threshold=120)
    await client.start(bot_token=cfg.bot_token)
    me = await client.get_me()
    log.info("Bot @%s 已登录，DC%s", me.username, client.session.dc_id)

    storage = Storage(client, cfg.channel_id, cfg.fav_channel_id)
    await storage.init()
    jm = JM()
    worker = Worker(cfg, db, jm, storage, client, me.username)
    bot = Bot(client, cfg, db, jm, worker, storage)
    bot.register()
    try:
        await bot.setup_menu()
    except Exception:
        log.warning("设置 Bot 菜单失败", exc_info=True)

    reader = Reader(db, storage, PageCache(cfg.cache_dir, cfg.cache_mb * 1024 * 1024))
    app = create_app(cfg, db, worker, reader)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.http_host, port=cfg.http_port,
                                           log_level="warning", proxy_headers=True))
    tasks = [asyncio.create_task(worker.run(), name="worker"),
             asyncio.create_task(client.run_until_disconnected(), name="telegram")]
    log.info("阅读页监听 %s:%s", cfg.http_host, cfg.http_port)
    try:
        await server.serve()
    finally:
        for t in tasks:
            t.cancel()
        await client.disconnect()


def enqueue(cfg, kind: str, jm_id: int):
    db = DB(cfg.db_path)
    job_id, created = db.add_job(kind, jm_id, cfg.owner_id, cfg.owner_id)
    print(f"任务 {job_id}{' 已创建' if created else ' 已存在'}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    cfg = config.load()
    args = sys.argv[1:]
    if args[:1] == ["enqueue"] and len(args) == 3 and args[1] in ("archive", "favorite") and args[2].isdigit():
        enqueue(cfg, args[1], int(args[2]))
    elif not args:
        asyncio.run(serve(cfg))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
