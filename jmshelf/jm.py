"""在主进程里调用 jmcomic 做搜索、取详情、下载封面（都很轻，放在线程里跑）。"""
import asyncio
import threading
import time
from pathlib import Path

import jmcomic
from jmcomic import JmcomicText

jmcomic.disable_jm_log()
ALBUM_TTL = 600

# 搜索方式 → JM 接口的 main_tag
SEARCH_BY = {"site": 0, "work": 1, "author": 2, "tag": 3, "actor": 4}
# 实测（2026-10-24）移动端接口真正生效的排序；评分 tr / 评论 md 结果相同，看不出是否生效，不提供
ORDERS = ("mr", "mv", "mp", "tf")
# 时间范围要拼进排序参数（mv_w、mv_m），而且只对“最多观看”有效
TIMES = ("a", "t", "w", "m")
# 分类浏览：cosplay、英文站在移动端会退回成“同人”“全部”，不提供
CATEGORIES = ("0", "doujin", "single", "short", "another", "hanman", "meiman", "3D")


def order_param(order: str, time_range: str) -> str:
    if order == "mv" and time_range in ("t", "w", "m"):
        return f"mv_{time_range}"
    return order


class JM:
    def __init__(self):
        self._client = None
        self._lock = threading.Lock()
        self._albums: dict[int, tuple[float, dict]] = {}

    def _cl(self):
        if self._client is None:
            self._client = jmcomic.JmOption.construct({"client": {"impl": "api"}}).new_jm_client()
        return self._client

    @staticmethod
    def _page(result) -> dict:
        items = [{"jm_id": int(aid), "title": info.get("name", ""), "author": info.get("author", "")}
                 for aid, info in result.content]
        return {"total": int(result.total), "pages": result.page_count, "items": items}

    def _search(self, query: str, page: int, by: str = "site", order: str = "mr", time_range: str = "a") -> dict:
        with self._lock:
            result = self._cl().search(query, page, SEARCH_BY.get(by, 0), order_param(order, time_range),
                                       "a", "0", None)
        return self._page(result)

    def _browse(self, page: int, category: str, order: str, time_range: str) -> dict:
        """分类浏览和排行榜（排行榜 = 最多观看 + 时间范围）。"""
        with self._lock:
            result = self._cl().categories_filter(page, time_range, category, order)
        return self._page(result)

    def _album(self, jm_id: int) -> dict:
        with self._lock:
            al = self._cl().get_album_detail(str(jm_id))
        chapters = [{"photo_id": int(pid), "sort": int(sort), "title": title or f"第{sort}章"}
                    for pid, sort, title in al.episode_list]
        chapters.sort(key=lambda c: c["sort"])
        return {
            "jm_id": int(al.album_id), "title": al.name, "authors": list(al.authors),
            "works": list(al.works), "tags": list(al.tags), "page_count": int(al.page_count or 0),
            "chapters": chapters,
        }

    def _cover(self, jm_id: int, path: Path):
        with self._lock:
            self._cl().download_album_cover(str(jm_id), str(path))

    def _photo_album(self, photo_id: int) -> int:
        with self._lock:
            photo = self._cl().get_photo_detail(str(photo_id), False, False)
        return int(photo.album_id)

    @staticmethod
    def _login(username: str, password: str):
        client = jmcomic.JmOption.construct({"client": {"impl": "api"}}).new_jm_client()
        client.login(username, password)
        return client

    @staticmethod
    def collect_ids(client, folder_id: str) -> tuple[list[int], list]:
        """读出某个收藏夹（"0" 表示全部收藏）的作品号，以及账号的收藏夹列表。"""
        ids, folders, page = [], [], 1
        while page <= 500:
            fav = client.favorite_folder(page=page, folder_id=folder_id)
            if page == 1:
                folders = list(fav.iter_folder_id_name())
            batch = [int(i) for i in fav.iter_id()]
            ids += [i for i in batch if i not in ids]
            if not batch or len(ids) >= int(fav.total):
                break
            page += 1
        return ids, folders

    @classmethod
    def _favorites(cls, username: str, password: str) -> dict:
        """登录 JM 账号，读出全部收藏。返回各收藏夹的作品号，以及不在任何收藏夹里的（未分类）。"""
        client = cls._login(username, password)
        everything, folders = cls.collect_ids(client, "0")
        result, placed = [], set()
        for fid, name in folders:
            ids, _ = cls.collect_ids(client, str(fid))
            placed.update(ids)
            result.append({"id": str(fid), "name": name, "items": ids})
        rest = [i for i in everything if i not in placed]
        return {"total": len(everything), "folders": result, "uncategorized": rest}

    @classmethod
    def push_with_client(cls, client, jm_ids: list[int]) -> dict:
        """把作品加进 JM 收藏（反向同步）。

        JM 移动端的收藏接口是“切换”：对已收藏的作品再调用会先取消再由 jmcomic 回滚，
        可能打乱它在 JM 收藏夹里的位置。所以先读一遍现有收藏，已收藏的直接跳过。
        移动端接口不支持指定收藏夹，新加的都进默认收藏。
        """
        have = set(cls.collect_ids(client, "0")[0])
        added, skipped, failed = [], [], []
        for jm_id in dict.fromkeys(jm_ids):
            if jm_id in have:
                skipped.append(jm_id)
                continue
            try:
                client.add_favorite_album(str(jm_id))
                added.append(jm_id)
            except Exception as e:
                if "已自动回滚" in str(e):
                    # jmcomic 发现其实已经收藏过，已回滚还原，等于已收藏
                    skipped.append(jm_id)
                else:
                    failed.append((jm_id, str(e)[:120]))
        return {"added": added, "skipped": skipped, "failed": failed}

    def _photo_pages(self, photo_id: int) -> int:
        with self._lock:
            return len(self._cl().get_photo_detail(str(photo_id), False, False))

    async def photo_pages(self, photo_id: int) -> int:
        """一章有多少页（入库时用来算下载进度）。"""
        return await asyncio.to_thread(self._photo_pages, photo_id)

    async def photo_album(self, photo_id: int) -> int:
        return await asyncio.to_thread(self._photo_album, photo_id)

    async def favorites(self, username: str, password: str) -> dict:
        return await asyncio.to_thread(self._favorites, username, password)

    async def push_favorites(self, username: str, password: str, jm_ids: list[int]) -> dict:
        return await asyncio.to_thread(lambda: self.push_with_client(self._login(username, password), jm_ids))

    async def search(self, query: str, page: int = 1, by: str = "site", order: str = "mr",
                     time_range: str = "a") -> dict:
        """by：site 关键词、work 作品、author 作者、tag 标签、actor 角色。每页 80 条。"""
        return await asyncio.to_thread(self._search, query, page, by, order, time_range)

    async def browse(self, page: int = 1, category: str = "0", order: str = "mr", time_range: str = "a") -> dict:
        return await asyncio.to_thread(self._browse, page, category, order, time_range)

    async def album(self, jm_id: int, fresh: bool = False) -> dict:
        """作品详情，缓存 10 分钟（Bot 和书架共用）；入库时用 fresh=True 取最新章节。"""
        hit = self._albums.get(jm_id)
        if hit and not fresh and time.time() - hit[0] < ALBUM_TTL:
            return hit[1]
        album = await asyncio.to_thread(self._album, jm_id)
        self._albums[jm_id] = (time.time(), album)
        return album

    def cached_album(self, jm_id: int) -> dict | None:
        hit = self._albums.get(jm_id)
        return hit[1] if hit else None

    @staticmethod
    def fetch_cover(jm_id: int) -> bytes:
        """直接从 JM 图片服务器取竖版封面（不经过接口客户端，可以并发）。"""
        from curl_cffi import requests

        for size in ("_3x4", ""):
            resp = requests.get(JmcomicText.get_album_cover_url(str(jm_id), size=size),
                                impersonate="chrome", timeout=15)
            if resp.status_code == 200 and resp.content[:3] == b"\xff\xd8\xff":
                return resp.content
        raise LookupError(f"JM{jm_id} 没有封面")

    async def cover(self, jm_id: int, path: Path):
        await asyncio.to_thread(self._cover, jm_id, path)
