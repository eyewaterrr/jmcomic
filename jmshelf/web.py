"""Mini App 后端：身份校验、书架 / 作品 / 章节接口、按页原图（JM → 服务器缓存 → 手机）、阅读进度、搜索。

v3：图片直接从 JM 取，服务器只缓存原图、不解码；阅读页按章节接口给的 num 用 Canvas 还原。
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import ids
from .jm import CATEGORIES, ORDERS, SEARCH_BY, TIMES

log = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).parent / "webapp"
TOKEN_TTL = 12 * 3600
INIT_DATA_TTL = 24 * 3600
JM_COVER_LIMIT = 5000
MIME = {".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}
IMAGE_CACHE = "private, max-age=86400, immutable"


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


def page_list(chapter: dict, dims: list) -> list[list[int]]:
    """阅读页用的每页信息：[num, 宽, 高]；尺寸还不知道时为 0（阅读页先用占位比例排版）。"""
    out = []
    for i, page in enumerate(chapter["pages"]):
        size = dims[i] if i < len(dims) and dims[i] else (0, 0)
        out.append([page["num"], size[0], size[1]])
    return out


# ---- 应用 ----
def create_app(cfg, db, worker, cache, dims_recorder=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    key = hmac.new(b"jmshelf-session", cfg.bot_token.encode(), hashlib.sha256).digest()
    index_html = (WEBAPP_DIR / "index.html").read_text()
    version = str(int(max(p.stat().st_mtime for p in (WEBAPP_DIR / "static").iterdir())))
    jm = worker.jm

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

    def jm_id_of(data: dict) -> int:
        try:
            return int(data.get("jmId"))
        except (TypeError, ValueError):
            raise HTTPException(400, "缺少参数")

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

    def pick_state(jm_id: int) -> str:
        if db.pick(jm_id):
            return "done"
        return "running" if db.active_job("publish", jm_id) else "none"

    def pick_link(jm_id: int) -> str | None:
        row = db.pick(jm_id)
        return worker.storage.post_link(worker.storage.fav_channel_id, int(row["msg_ids"].split(",")[0])) if row else None

    # ---- 书架 ----
    @app.get("/api/library")
    async def api_library(request: Request):
        """收藏的作品 + 读过的作品。"""
        uid = auth(request)
        items = []
        for a in db.shelf(uid):
            read = db.chapter(a["p_photo"]) if a["p_photo"] else None
            folder_ids = [int(x) for x in a["folder_ids"].split(",")] if a["folder_ids"] else []
            items.append({
                "jmId": a["jm_id"], "title": a["title"], "chapters": a["chapter_count"], "pages": a["pages"],
                "fav": bool(folder_ids), "folders": folder_ids, "pick": bool(a["pick"]),
                "progress": {"photoId": a["p_photo"], "page": a["p_page"], "sort": read["sort"] if read else 1}
                if a["p_photo"] else None,
            })
        return {"items": items, "folders": folder_list(uid)}

    # ---- 作品（书架里的和搜索结果里的是同一个页面） ----
    def album_from_db(jm_id: int) -> dict | None:
        row = db.album(jm_id)
        if row is None or not row["title"]:
            return None
        return {"jm_id": jm_id, "title": row["title"], "authors": json.loads(row["authors"] or "[]"),
                "works": json.loads(row["works"] or "[]"), "tags": json.loads(row["tags"] or "[]"),
                "page_count": 0,
                "chapters": [{"photo_id": c["photo_id"], "sort": c["sort"], "title": c["title"]}
                             for c in db.chapters(jm_id)]}

    @app.get("/api/album/{jm_id}")
    async def api_album(jm_id: int, request: Request, fresh: int = 0):
        uid = auth(request)
        offline = False
        try:
            a = await jm.album(jm_id, bool(fresh))
            if db.album(jm_id):
                db.upsert_album(a)  # 收藏或读过的作品顺便更新元数据
        except Exception:
            a = album_from_db(jm_id)
            if a is None:
                raise HTTPException(404, f"找不到 JM{jm_id}，或者暂时取不到详情")
            offline = True
        known = {c["photo_id"]: c for c in db.chapters(jm_id)}
        member = {f["id"] for f in db.album_folders(uid, jm_id)}
        p = db.progress(uid, jm_id)
        return {
            "jmId": jm_id, "title": a["title"], "authors": a["authors"], "works": a["works"], "tags": a["tags"],
            "pageCount": a["page_count"], "offline": offline,
            "chapters": [{
                "photoId": c["photo_id"], "sort": c["sort"], "title": c["title"],
                "pages": known[c["photo_id"]]["page_count"] if c["photo_id"] in known else 0,
                "cached": cache.cached_pages(c["photo_id"]),
            } for c in a["chapters"]],
            "fav": bool(member),
            "folders": [dict(f, member=f["id"] in member) for f in folder_list(uid)],
            "pick": pick_state(jm_id), "pickLink": pick_link(jm_id),
            "pickProgress": worker.progress_for("publish", jm_id),
            "progress": {"photoId": p["photo_id"], "page": p["page"]} if p else None,
        }

    @app.post("/api/album/{jm_id}/folders")
    async def api_album_folders(jm_id: int, request: Request):
        """设置作品在本地收藏夹里的归属（只改收藏夹，不触发任何任务）。"""
        uid = auth(request)
        was = db.is_favorited(uid, jm_id)
        try:
            wanted = {int(x) for x in (await body(request)).get("folderIds", [])}
            db.set_album_folders(uid, jm_id, wanted)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e) or "参数不对")
        now = db.is_favorited(uid, jm_id)
        if now:
            worker.remember(jm_id, jm.cached_album(jm_id))
        if not was and now:
            worker.queue_jm_push(uid, [jm_id])
        worker.save_meta(jm_id)
        return {"ok": True, "fav": now}

    @app.post("/api/favorite")
    async def api_favorite(request: Request):
        """收藏到默认收藏夹。"""
        uid = auth(request)
        await worker.favorite(uid, jm_id_of(await body(request)))
        return {"ok": True}

    @app.post("/api/unfavorite")
    async def api_unfavorite(request: Request):
        uid = auth(request)
        jm_id = jm_id_of(await body(request))
        db.unfavorite(uid, jm_id)
        worker.save_meta(jm_id)
        return {"ok": True}

    @app.post("/api/publish")
    async def api_publish(request: Request):
        """上传精选：生成 PDF 发到精选频道。返回 done（已经上传过）/ created / exists。"""
        uid = auth(request)
        return {"result": await worker.publish(uid, jm_id_of(await body(request)))}

    # ---- 收藏夹管理 ----
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

    # ---- 阅读 ----
    async def chapter_detail(photo_id: int) -> dict:
        try:
            return await jm.chapter(photo_id)
        except Exception as e:
            log.info("取章节 %s 失败：%s", photo_id, e)
            raise HTTPException(502, "JM 暂时取不到这一章，请稍后再试")

    @app.get("/api/chapter/{photo_id}")
    async def api_chapter(photo_id: int, request: Request):
        uid = auth(request)
        ch = await chapter_detail(photo_id)
        jm_id = ch["jm_id"]
        try:
            album = await jm.album(jm_id)
            worker.remember(jm_id, album)
            chapters = album["chapters"]
            title = album["title"]
        except Exception:
            worker.remember(jm_id)
            chapters = [{"photo_id": c["photo_id"], "sort": c["sort"], "title": c["title"]} for c in db.chapters(jm_id)]
            row = db.album(jm_id)
            title = row["title"] if row else ""
        if not any(c["photo_id"] == photo_id for c in chapters):
            chapters = [{"photo_id": photo_id, "sort": ch["sort"], "title": ch["title"]}]
        db.set_chapter_pages(photo_id, len(ch["pages"]))
        cache.touch(photo_id)
        dims = db.chapter_dims(photo_id)
        if dims_recorder and photo_id in dims_recorder.pending:
            dims = dims + [None] * (len(ch["pages"]) - len(dims))
            for n, size in dims_recorder.pending[photo_id].items():
                if n <= len(dims):
                    dims[n - 1] = size
        siblings = [c["photo_id"] for c in chapters]
        pos = siblings.index(photo_id)
        me = chapters[pos]
        p = db.progress(uid, jm_id)
        return {
            "photoId": photo_id, "jmId": jm_id, "albumTitle": title,
            "title": me["title"], "sort": me["sort"], "chapterCount": len(siblings), "index": pos + 1,
            "prev": siblings[pos - 1] if pos > 0 else None,
            "next": siblings[pos + 1] if pos + 1 < len(siblings) else None,
            "pages": page_list(ch, dims),
            "page": p["page"] if p and p["photo_id"] == photo_id else 1,
        }

    @app.get("/img/{photo_id}/{n}")
    async def img(photo_id: int, n: int, request: Request):
        """一页 JM 原图（还没还原）。缓存里没有时先下载，超过 30 秒返回 503，阅读页稍后重试。"""
        auth(request)
        ch = await chapter_detail(photo_id)
        if not 1 <= n <= len(ch["pages"]):
            raise HTTPException(404, "没有这一页")
        try:
            path = await cache.page(photo_id, n, ch)
        except asyncio.TimeoutError:
            raise HTTPException(503, "这一页还没下好")
        except Exception as e:
            log.info("从 JM 取 %s/%s 失败：%s", photo_id, n, e)
            jm.forget_chapter(photo_id)
            worker.request_update_check()
            raise HTTPException(502, "JM 暂时取不到这一页")
        cache.read_ahead(photo_id, n, ch)
        return FileResponse(path, media_type=MIME.get(path.suffix, "application/octet-stream"),
                            headers={"Cache-Control": IMAGE_CACHE})

    @app.post("/api/progress")
    async def api_progress(request: Request):
        uid = auth(request)
        data = await body(request)
        try:
            photo_id, page = int(data["photoId"]), int(data["page"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "缺少参数")
        chapter = db.chapter(photo_id)
        if chapter is None or page < 1 or (chapter["page_count"] and page > chapter["page_count"]):
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

    # ---- 搜索页、任务页（和 Bot 对话的功能相同） ----
    cover_sem = asyncio.Semaphore(6)
    jm_cover_dir = cfg.cache_dir / "jmcovers"

    def title_of(jm_id: int) -> str:
        row = db.album(jm_id)
        if row and row["title"]:
            return row["title"]
        cached = jm.cached_album(jm_id)
        return cached["title"] if cached else ""

    def decorate(uid: int, items: list[dict]) -> list[dict]:
        states = db.album_states(uid, [it["jmId"] for it in items])
        for it in items:
            st = states[it["jmId"]]
            it.update(fav=st["fav"], pick="done" if st["pick"] else "running" if st["picking"] else "none")
        return items

    def id_list(data: dict) -> list[int]:
        try:
            result = list(dict.fromkeys(int(x) for x in data.get("jmIds", [])))
        except (TypeError, ValueError):
            raise HTTPException(400, "JM 号格式不对")
        if not result or len(result) > 50:
            raise HTTPException(400, "一次最多 50 本")
        return result

    @app.get("/api/jm/search")
    async def api_jm_search(request: Request, q: str = "", page: int = 1, by: str = "site",
                            order: str = "mr", time: str = "a"):
        """by：site 关键词（能识别 JM 号，和给 Bot 发消息一样）、work 作品、author 作者、tag 标签、actor 角色。
        order：mr 最新、mv 最多观看、mp 最多图片、tf 最多点赞；time 只对最多观看有效。每页 80 条。"""
        uid = auth(request)
        q = q.strip()
        by = by if by in SEARCH_BY else "site"
        order = order if order in ORDERS else "mr"
        time = time if time in TIMES else "a"
        if not q:
            raise HTTPException(400, "请输入关键词或 JM 号")
        found = ids.extract(q) if by == "site" else []
        if found:
            jm_ids = []
            for kind, number in found:
                try:
                    jm_id = await jm.photo_album(number) if kind == "photo" else number
                except Exception:
                    continue
                if jm_id not in jm_ids:
                    jm_ids.append(jm_id)
            items = [{"jmId": i, "title": title_of(i), "author": ""} for i in jm_ids]
            return {"mode": "ids", "items": decorate(uid, items), "total": len(items), "page": 1, "pages": 1}
        try:
            res = await jm.search(q, max(page, 1), by, order, time)
        except Exception as e:
            log.info("搜索 %s 失败：%s", q, e)
            raise HTTPException(502, "JM 搜索暂时不可用，请稍后再试")
        items = [{"jmId": it["jm_id"], "title": it["title"], "author": it["author"]} for it in res["items"]]
        return {"mode": "search", "items": decorate(uid, items),
                "total": res["total"], "page": page, "pages": res["pages"]}

    @app.get("/api/jm/browse")
    async def api_jm_browse(request: Request, category: str = "0", order: str = "mr", time: str = "a",
                            page: int = 1):
        """分类浏览；排行榜用 order=mv 加 time（t 日、w 周、m 月、a 总榜）。"""
        uid = auth(request)
        if category not in CATEGORIES or order not in ORDERS or time not in TIMES:
            raise HTTPException(400, "分类或排序参数不对")
        try:
            res = await jm.browse(max(page, 1), category, order, time)
        except Exception as e:
            log.info("分类浏览失败：%s", e)
            raise HTTPException(502, "JM 暂时不可用，请稍后再试")
        items = [{"jmId": it["jm_id"], "title": it["title"], "author": it["author"]} for it in res["items"]]
        return {"mode": "browse", "items": decorate(uid, items), "total": res["total"],
                "page": page, "pages": res["pages"]}

    @app.post("/api/jm/favorite")
    async def api_jm_favorite(request: Request):
        """一起收藏到默认收藏夹（多个 JM 号）。"""
        uid = auth(request)
        await worker.favorite_many(uid, id_list(await body(request)))
        return {"ok": True}

    @app.get("/api/jobs")
    async def api_jobs(request: Request):
        auth(request)
        items = [{
            "id": j["id"], "kind": j["kind"], "jmId": j["jm_id"], "title": title_of(j["jm_id"]),
            "status": j["status"], "step": j["step"], "error": j["error"], "attempts": j["attempts"],
            "retryAt": j["run_after"] if j["status"] == "queued" and j["run_after"] > time.time() else 0,
            "updatedAt": j["updated_at"],
            "percent": worker.live.get(j["id"], {}).get("percent") if j["status"] == "running" else None,
        } for j in db.recent_jobs(40)]
        counts = db.job_counts()
        return {"items": items, "queued": counts.get("queued", 0), "running": counts.get("running", 0)}

    def trim_jm_covers():
        files = sorted(jm_cover_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        for p in files[:max(0, len(files) - JM_COVER_LIMIT + 1000)]:
            p.unlink(missing_ok=True)

    @app.get("/jmcover/{jm_id}")
    async def jm_cover(jm_id: int, request: Request):
        """封面：收藏或读过的用本地封面，其余由服务器代取 JM 竖版封面并缓存（手机直连 JM 很慢）。"""
        auth(request)
        headers = {"Cache-Control": "private, max-age=86400"}
        local = cfg.cover_dir / f"{jm_id}.jpg"
        if local.exists():
            return FileResponse(local, media_type="image/jpeg", headers=headers)
        path = jm_cover_dir / f"{jm_id}.jpg"
        if not path.exists():
            async with cover_sem:
                if not path.exists():
                    try:
                        data = await asyncio.to_thread(jm.fetch_cover, jm_id)
                    except Exception:
                        raise HTTPException(404, "封面取不到")
                    jm_cover_dir.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(data)
                    tmp.replace(path)
                    if len(list(jm_cover_dir.glob("*.jpg"))) > JM_COVER_LIMIT:
                        trim_jm_covers()
        return FileResponse(path, media_type="image/jpeg", headers=headers)

    app.mount("/static", StaticFiles(directory=WEBAPP_DIR / "static"), name="static")
    return app
