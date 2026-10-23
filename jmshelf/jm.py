"""在主进程里调用 jmcomic 做搜索、取详情、下载封面（都很轻，放在线程里跑）。"""
import asyncio
import threading
from pathlib import Path

import jmcomic

jmcomic.disable_jm_log()


class JM:
    def __init__(self):
        self._client = None
        self._lock = threading.Lock()

    def _cl(self):
        if self._client is None:
            self._client = jmcomic.JmOption.construct({"client": {"impl": "api"}}).new_jm_client()
        return self._client

    def _search(self, query: str, page: int) -> dict:
        with self._lock:
            result = self._cl().search_site(search_query=query, page=page)
        items = [{"jm_id": int(aid), "title": info.get("name", ""), "author": info.get("author", "")}
                 for aid, info in result.content]
        return {"total": int(result.total), "pages": result.page_count, "items": items}

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
    def _favorites(username: str, password: str) -> dict:
        """登录 JM 账号，读出全部收藏。返回各收藏夹的作品号，以及不在任何收藏夹里的（未分类）。"""
        client = jmcomic.JmOption.construct({"client": {"impl": "api"}}).new_jm_client()
        client.login(username, password)

        def collect(folder_id: str):
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

        everything, folders = collect("0")
        result, placed = [], set()
        for fid, name in folders:
            ids, _ = collect(str(fid))
            placed.update(ids)
            result.append({"id": str(fid), "name": name, "items": ids})
        rest = [i for i in everything if i not in placed]
        return {"total": len(everything), "folders": result, "uncategorized": rest}

    async def photo_album(self, photo_id: int) -> int:
        return await asyncio.to_thread(self._photo_album, photo_id)

    async def favorites(self, username: str, password: str) -> dict:
        return await asyncio.to_thread(self._favorites, username, password)

    async def search(self, query: str, page: int = 1) -> dict:
        return await asyncio.to_thread(self._search, query, page)

    async def album(self, jm_id: int) -> dict:
        return await asyncio.to_thread(self._album, jm_id)

    async def cover(self, jm_id: int, path: Path):
        await asyncio.to_thread(self._cover, jm_id, path)
