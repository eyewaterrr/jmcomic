"""jmcomic 自检：搜索、取作品和章节详情、下载两张原图并还原。退出码 0 表示通过。

走的是服务实际用的代码（jmshelf.jm、jmshelf.images）。自动更新脚本（deploy/jmcomic-update.sh）
用它判断新版本能不能用：
    python -m jmshelf.selftest
"""
import importlib.metadata
import io
import sys
import time

from PIL import Image

from .images import descramble
from .jm import JM


def main() -> int:
    started = time.time()
    try:
        jm = JM()
        found = jm._search("原神", 1)["items"][:3]
        assert found, "搜索没有结果"
        album = jm._album(found[0]["jm_id"])
        assert album["chapters"], "作品没有章节"
        chapter = jm._chapter(album["chapters"][0]["photo_id"])
        assert chapter["pages"], "章节没有图片"
        for page in chapter["pages"][:2]:
            with Image.open(io.BytesIO(jm.image_bytes(page["url"]))) as im:
                im.load()
                out = descramble(im.convert("RGB"), page["num"])
                assert out.size[0] > 0 and out.size == im.size
    except Exception as e:
        print(f"FAIL jmcomic {importlib.metadata.version('jmcomic')}: {type(e).__name__}: {e}")
        return 1
    print(f"OK jmcomic {importlib.metadata.version('jmcomic')} JM{found[0]['jm_id']} "
          f"{len(chapter['pages'])} 页 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
