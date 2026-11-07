"""启动服务：python -m jmshelf

运维用的子命令（服务运行时也能用）：
    python -m jmshelf enqueue publish <JM号>...       上传精选（跳过已上传和已在队列的），由服务里的队列执行
    python -m jmshelf export-meta                      从数据库重新生成每本的元数据 JSON
    python -m jmshelf restore-meta <目录>               用元数据 JSON 恢复数据库（先停服务）
"""
import asyncio
import logging
import shutil
import sys
from pathlib import Path

import uvicorn
from telethon import TelegramClient

from . import config, meta
from .bot import Bot
from .cache import DimsRecorder, PageCache
from .db import DB
from .jm import JM
from .jobs import Worker
from .tg import Storage
from .web import create_app

log = logging.getLogger("jmshelf")
META_VERSION = "2"


def prepare(cfg) -> DB:
    """打开数据库（需要时自动迁移到 v3），重新导出元数据，删掉 v1.1 的旧页面缓存。"""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = DB(cfg.db_path)
    db.ensure_owner(cfg.owner_id)
    if db.get_meta("meta_version") != META_VERSION or not any(meta.meta_dir(cfg.data_dir).glob("JM*.json")):
        log.info("已导出 %s 本作品的元数据", meta.export_all(db, cfg.data_dir))
        db.set_meta("meta_version", META_VERSION)
    if cfg.cache_dir.is_dir():
        # v1.1 的页面缓存是 cache/<章节ID>/，存的是从频道读回的 WebP，v3 用不上
        old = [d for d in cfg.cache_dir.iterdir() if d.is_dir() and d.name.isdigit()]
        for d in old:
            shutil.rmtree(d, ignore_errors=True)
        if old:
            log.info("已删除 v1.1 的旧页面缓存 %s 个目录", len(old))
    return db


async def serve(cfg):
    db = prepare(cfg)
    client = TelegramClient(str(cfg.session_path), cfg.api_id, cfg.api_hash,
                            use_ipv6=False, flood_sleep_threshold=120)
    await client.start(bot_token=cfg.bot_token)
    me = await client.get_me()
    log.info("Bot @%s 已登录，DC%s", me.username, client.session.dc_id)

    storage = Storage(client, cfg.fav_channel_id)
    await storage.init()
    jm = JM()
    dims = DimsRecorder(db)
    cache = PageCache(cfg.page_dir, jm, cfg.cache_mb * 1024 * 1024, cfg.cache_days, cfg.read_ahead,
                      cfg.image_threads, on_dims=dims)
    worker = Worker(cfg, db, jm, storage, client, me.username, cache)
    bot = Bot(client, cfg, db, jm, worker, storage)
    bot.register()
    try:
        await bot.setup_menu()
    except Exception:
        log.warning("设置 Bot 菜单失败", exc_info=True)

    app = create_app(cfg, db, worker, cache, dims)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.http_host, port=cfg.http_port,
                                           log_level="warning", proxy_headers=True))
    tasks = [asyncio.create_task(worker.run(), name="worker"),
             asyncio.create_task(client.run_until_disconnected(), name="telegram"),
             asyncio.create_task(cache.cleanup_loop(), name="cache-cleanup"),
             asyncio.create_task(dims.loop(), name="dims")]
    log.info("阅读页监听 %s:%s；缓存上限 %sMB，%s 天没打开的章节删除，预读 %s 页",
             cfg.http_host, cfg.http_port, cfg.cache_mb, cfg.cache_days, cfg.read_ahead)
    try:
        await server.serve()
    finally:
        for t in tasks:
            t.cancel()
        dims.flush()
        await client.disconnect()


def enqueue_publish(cfg, jm_ids: list[int]):
    db = DB(cfg.db_path)
    todo, skipped = [], []
    for jm_id in dict.fromkeys(jm_ids):
        (skipped if db.pick(jm_id) or db.active_job("publish", jm_id) else todo).append(jm_id)
    for jm_id in todo:
        db.add_job("publish", jm_id, cfg.owner_id, cfg.owner_id)
    print(f"新建 {len(todo)} 个上传精选任务 {todo}；跳过 {len(skipped)} 本（已上传或已在队列）{skipped}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    cfg = config.load()
    args = sys.argv[1:]
    if args[:2] == ["enqueue", "publish"] and len(args) >= 3 and all(a.isdigit() for a in args[2:]):
        enqueue_publish(cfg, [int(a) for a in args[2:]])
    elif args == ["export-meta"]:
        print(f"已导出 {meta.export_all(DB(cfg.db_path), cfg.data_dir)} 本，目录 {meta.meta_dir(cfg.data_dir)}")
    elif args[:1] == ["restore-meta"] and len(args) == 2:
        db = DB(cfg.db_path)
        db.ensure_owner(cfg.owner_id)
        print(f"已恢复 {meta.restore_all(db, Path(args[1]))} 本")
    elif not args:
        asyncio.run(serve(cfg))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
