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

    async def search(self, query: str, page: int = 1) -> dict:
        return await asyncio.to_thread(self._search, query, page)

    async def album(self, jm_id: int) -> dict:
        return await asyncio.to_thread(self._album, jm_id)

    async def cover(self, jm_id: int, path: Path):
        await asyncio.to_thread(self._cover, jm_id, path)
