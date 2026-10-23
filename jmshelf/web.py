"""Mini App 后端：身份校验、书架 / 作品 / 章节接口、按页图片（磁盘缓存 + 预取）、阅读进度。"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).parent / "webapp"
TOKEN_TTL = 12 * 3600
INIT_DATA_TTL = 24 * 3600
PREFETCH_PAGES = 6
MIME = {".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}


# ---- 身份 ----
def check_init_data(init_data: str, bot_token: str, max_age: int = INIT_DATA_TTL, now: float | None = None):
    """校验 Telegram Mini App 的 initData，通过时返回 {"user": {...}, "start_param": "..."}。"""
    if not init_data:
        return None
    fields = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", "")
    if not received:
        return None
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()

    def digest(data: dict) -> str:
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()

    ok = hmac.compare_digest(digest(fields), received)
    if not ok and "signature" in fields:
        ok = hmac.compare_digest(digest({k: v for k, v in fields.items() if k != "signature"}), received)
    if not ok:
        return None
    auth_date = int(fields.get("auth_date", "0") or 0)
    if (now or time.time()) - auth_date > max_age:
        return None
    try:
        user = json.loads(fields.get("user", "{}"))
    except ValueError:
        return None
    if "id" not in user:
        return None
    return {"user": user, "start_param": fields.get("start_param", "")}


def make_token(key: bytes, user_id: int, ttl: int = TOKEN_TTL, now: float | None = None) -> str:
    payload = f"{user_id}.{int((now or time.time()) + ttl)}"
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def read_token(key: bytes, token: str, now: float | None = None) -> int | None:
    try:
        uid, exp, sig = token.split(".")
        expected = hmac.new(key, f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(sig, expected) and int(exp) > (now or time.time()):
            return int(uid)
    except (ValueError, AttributeError):
        pass
    return None


# ---- 页面缓存 ----
class PageCache:
    """按页的磁盘缓存，超过上限时删掉最久没用过的文件，降到上限的 90%。"""

    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.max_bytes = max_bytes
        self.total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0

    def _path(self, photo_id: int, idx: int, ext: str) -> Path:
        return self.root / str(photo_id) / f"{idx:04d}{ext}"

    def get(self, photo_id: int, idx: int, ext: str) -> bytes | None:
        path = self._path(photo_id, idx, ext)
        try:
            data = path.read_bytes()
            os.utime(path)
            return data
        except FileNotFoundError:
            return None

    def has(self, photo_id: int, idx: int, ext: str) -> bool:
        return self._path(photo_id, idx, ext).exists()

    def put(self, photo_id: int, idx: int, ext: str, data: bytes):
        path = self._path(photo_id, idx, ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        self.total += len(data)
        if self.total > self.max_bytes:
            self._evict()

    def _evict(self):
        files = sorted((p for p in self.root.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime)
        self.total = sum(p.stat().st_size for p in files)
        target = int(self.max_bytes * 0.9)
        for p in files:
            if self.total <= target:
                break
            size = p.stat().st_size
            p.unlink(missing_ok=True)
            self.total -= size


class Reader:
    """取单页：先查缓存，没有就从频道按区间读取；同一页的并发请求只读一次。"""

    def __init__(self, db, storage, cache: PageCache):
        self.db = db
        self.storage = storage
        self.cache = cache
        self._inflight: dict[tuple[int, int], asyncio.Task] = {}
        self._prefetch_sem = asyncio.Semaphore(3)

    async def page(self, photo_id: int, idx: int) -> tuple[bytes, str]:
        row = self.db.page(photo_id, idx)
        if row is None:
            raise KeyError
        ext = Path(row["name"]).suffix.lower()
        data = self.cache.get(photo_id, idx, ext)
        if data is None:
            data = await asyncio.shield(self._fetch(photo_id, idx, row, ext))
        return data, ext

    def _fetch(self, photo_id, idx, row, ext) -> asyncio.Task:
        key = (photo_id, idx)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._read(photo_id, idx, row, ext))
            self._inflight[key] = task
            task.add_done_callback(lambda _: self._inflight.pop(key, None))
        return task

    async def _read(self, photo_id, idx, row, ext) -> bytes:
        chapter = self.db.chapter(photo_id)
        data = await self.storage.read_range(chapter["msg_id"], row["offset"], row["size"])
        self.cache.put(photo_id, idx, ext, data)
        return data

    def prefetch(self, photo_id: int, idx: int, count: int = PREFETCH_PAGES):
        for n in range(idx + 1, idx + 1 + count):
            row = self.db.page(photo_id, n)
            if row is None:
                break
            ext = Path(row["name"]).suffix.lower()
            if (photo_id, n) in self._inflight or self.cache.has(photo_id, n, ext):
                continue
            asyncio.create_task(self._prefetch_one(photo_id, n, row, ext))

    async def _prefetch_one(self, photo_id, idx, row, ext):
        async with self._prefetch_sem:
            try:
                await self._fetch(photo_id, idx, row, ext)
            except Exception:
                log.debug("预取 %s/%s 失败", photo_id, idx, exc_info=True)


# ---- 应用 ----
def create_app(cfg, db, worker, reader: Reader) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    key = hmac.new(b"jmshelf-session", cfg.bot_token.encode(), hashlib.sha256).digest()
    index_html = (WEBAPP_DIR / "index.html").read_text()
    version = str(int(max(p.stat().st_mtime for p in (WEBAPP_DIR / "static").iterdir())))

    def auth(request: Request) -> int:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header else request.query_params.get("t", "")
        uid = read_token(key, token)
        if uid is None or db.user(uid) is None:
            raise HTTPException(401, "未登录或登录已过期")
        return uid

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, "请求格式不对")
        if not isinstance(data, dict):
            raise HTTPException(400, "请求格式不对")
        return data

    @app.middleware("http")
    async def no_store_api(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(index_html.replace("__VERSION__", version), headers={"Cache-Control": "no-cache"})

    @app.post("/api/auth")
    async def api_auth(request: Request):
        data = await body(request)
        info = check_init_data(str(data.get("initData", "")), cfg.bot_token)
        if not info:
            raise HTTPException(401, "身份校验失败")
        uid = int(info["user"]["id"])
        row = db.user(uid)
        if row is None:
            raise HTTPException(403, "这个账号不在白名单里")
        return {
            "token": make_token(key, uid),
            "user": {"id": uid, "readMode": row["read_mode"], "owner": row["role"] == "owner"},
            "startParam": info["start_param"],
        }

    def folder_list(uid: int) -> list[dict]:
        return [{"id": f["id"], "name": f["name"], "source": f["source"], "default": bool(f["is_default"]),
                 "count": f["count"]} for f in db.folders(uid)]

    @app.get("/api/library")
    async def api_library(request: Request):
        uid = auth(request)
        progress = db.all_progress(uid)
        items = []
        for a in db.albums(uid):
            p = progress.get(a["jm_id"])
            read = db.chapter(p["photo_id"]) if p else None
            folder_ids = [int(x) for x in a["folder_ids"].split(",")] if a["folder_ids"] else []
            items.append({
                "jmId": a["jm_id"], "title": a["title"], "chapters": a["chapter_count"],
                "archived": a["archived"], "pages": a["pages"], "fav": bool(folder_ids),
                "folders": folder_ids, "pdf": bool(a["pdf"]),
                "progress": {"sort": read["sort"], "page": p["page"]} if read else None,
                "cover": (cfg.cover_dir / f"{a['jm_id']}.jpg").exists(),
            })
        return {"items": items, "folders": folder_list(uid)}

    @app.get("/api/album/{jm_id}")
    async def api_album(jm_id: int, request: Request):
        uid = auth(request)
        album = db.album(jm_id)
        if album is None:
            raise HTTPException(404, "书库里没有这本")
        p = db.progress(uid, jm_id)
        member = {f["id"] for f in db.album_folders(uid, jm_id)}
        return {
            "jmId": jm_id, "title": album["title"], "authors": json.loads(album["authors"]),
            "tags": json.loads(album["tags"]), "chapterCount": album["chapter_count"],
            "fav": bool(member),
            "folders": [dict(f, member=f["id"] in member) for f in folder_list(uid)],
            "pdf": "done" if db.pdf_record(jm_id) else "running" if db.active_job("favorite", jm_id) else "none",
            "cover": (cfg.cover_dir / f"{jm_id}.jpg").exists(),
            "chapters": [{"photoId": c["photo_id"], "sort": c["sort"], "title": c["title"],
                          "pages": c["page_count"]} for c in db.chapters(jm_id)],
            "progress": {"photoId": p["photo_id"], "page": p["page"]} if p else None,
        }

    @app.post("/api/album/{jm_id}/folders")
    async def api_album_folders(jm_id: int, request: Request):
        """设置作品在本地收藏夹里的归属。由不收藏变成收藏时自动安排入库或生成 PDF。"""
        uid = auth(request)
        if db.album(jm_id) is None:
            raise HTTPException(404, "书库里没有这本")
        was = db.is_favorited(uid, jm_id)
        try:
            wanted = {int(x) for x in (await body(request)).get("folderIds", [])}
            db.set_album_folders(uid, jm_id, wanted)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e) or "参数不对")
        worker.save_meta(jm_id)
        if not was and db.is_favorited(uid, jm_id):
            worker.queue_jm_push(uid, [jm_id])
        result = await worker.after_favorite(uid, jm_id) if db.is_favorited(uid, jm_id) else "none"
        return {"ok": True, "fav": db.is_favorited(uid, jm_id), "next": result}

    @app.get("/api/folders")
    async def api_folders(request: Request):
        return {"folders": folder_list(auth(request))}

    @app.post("/api/folders")
    async def api_folder_create(request: Request):
        uid = auth(request)
        try:
            folder_id = db.create_folder(uid, (await body(request)).get("name", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": folder_id, "folders": folder_list(uid)}

    @app.post("/api/folders/{folder_id}/rename")
    async def api_folder_rename(folder_id: int, request: Request):
        uid = auth(request)
        try:
            db.rename_folder(uid, folder_id, (await body(request)).get("name", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"folders": folder_list(uid)}

    @app.post("/api/folders/{folder_id}/delete")
    async def api_folder_delete(folder_id: int, request: Request):
        uid = auth(request)
        try:
            db.delete_folder(uid, folder_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"folders": folder_list(uid)}

    @app.get("/api/chapter/{photo_id}")
    async def api_chapter(photo_id: int, request: Request):
        uid = auth(request)
        chapter = db.chapter(photo_id)
        if chapter is None:
            raise HTTPException(404, "没有这一章")
        album = db.album(chapter["jm_id"])
        siblings = [c["photo_id"] for c in db.chapters(chapter["jm_id"])]
        pos = siblings.index(photo_id)
        p = db.progress(uid, chapter["jm_id"])
        return {
            "photoId": photo_id, "jmId": chapter["jm_id"], "albumTitle": album["title"],
            "title": chapter["title"], "sort": chapter["sort"], "chapterCount": len(siblings),
            "index": pos + 1,
            "prev": siblings[pos - 1] if pos > 0 else None,
            "next": siblings[pos + 1] if pos + 1 < len(siblings) else None,
            "pages": [[r["width"], r["height"]] for r in db.pages(photo_id)],
            "page": p["page"] if p and p["photo_id"] == photo_id else 1,
        }

    @app.get("/img/{photo_id}/{idx}")
    async def img(photo_id: int, idx: int, request: Request):
        auth(request)
        try:
            data, ext = await reader.page(photo_id, idx)
        except KeyError:
            raise HTTPException(404, "没有这一页")
        reader.prefetch(photo_id, idx)
        return Response(data, media_type=MIME.get(ext, "application/octet-stream"),
                        headers={"Cache-Control": "private, max-age=604800, immutable"})

    @app.get("/cover/{jm_id}")
    async def cover(jm_id: int, request: Request):
        auth(request)
        path = cfg.cover_dir / f"{jm_id}.jpg"
        if not path.exists():
            raise HTTPException(404, "没有封面")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/progress")
    async def api_progress(request: Request):
        uid = auth(request)
        data = await body(request)
        try:
            photo_id, page = int(data["photoId"]), int(data["page"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "缺少参数")
        chapter = db.chapter(photo_id)
        if chapter is None or not 1 <= page <= chapter["page_count"]:
            raise HTTPException(400, "页码不对")
        db.save_progress(uid, chapter["jm_id"], photo_id, page)
        return {"ok": True}

    @app.post("/api/settings")
    async def api_settings(request: Request):
        uid = auth(request)
        mode = (await body(request)).get("readMode")
        if mode not in ("scroll", "page"):
            raise HTTPException(400, "阅读方式只能是 scroll 或 page")
        db.set_read_mode(uid, mode)
        return {"ok": True}

    @app.post("/api/favorite")
    async def api_favorite(request: Request):
        """收藏到默认收藏夹；返回后续动作：archive（先入库）/ pdf（生成 PDF）/ done。"""
        uid = auth(request)
        try:
            jm_id = int((await body(request)).get("jmId"))
        except (TypeError, ValueError):
            raise HTTPException(400, "缺少参数")
        return {"next": await worker.favorite(uid, jm_id)}

    @app.post("/api/unfavorite")
    async def api_unfavorite(request: Request):
        uid = auth(request)
        try:
            jm_id = int((await body(request)).get("jmId"))
        except (TypeError, ValueError):
            raise HTTPException(400, "缺少参数")
        db.unfavorite(uid, jm_id)
        worker.save_meta(jm_id)
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=WEBAPP_DIR / "static"), name="static")
    return app
