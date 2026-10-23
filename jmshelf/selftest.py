"""jmcomic 自检：搜索、取详情、下载并解码两张图片。退出码 0 表示通过。

自动更新脚本（deploy/jmcomic-update.sh）用它判断新版本能不能用：
    python -m jmshelf.selftest
"""
import importlib.metadata
import sys
import tempfile
import time
from pathlib import Path

import jmcomic
from PIL import Image


def main() -> int:
    jmcomic.disable_jm_log()
    started = time.time()
    try:
        client = jmcomic.JmOption.construct({"client": {"impl": "api"}}).new_jm_client()
        found = [aid for aid, _ in client.search_site(search_query="原神", page=1).content][:3]
        assert found, "搜索没有结果"
        album = client.get_album_detail(str(found[0]))
        photo = client.get_photo_detail(str(album.episode_list[0][0]), False, True)
        assert len(photo) > 0, "章节没有图片"
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(min(2, len(photo))):
                image = photo[i]
                suffix = Path(image.download_url.split("?")[0]).suffix or ".webp"
                target = Path(tmp) / f"{i}{suffix}"
                client.download_by_image_detail(image, str(target))
                with Image.open(target) as im:
                    im.load()
                    assert im.size[0] > 0
    except Exception as e:
        print(f"FAIL jmcomic {importlib.metadata.version('jmcomic')}: {type(e).__name__}: {e}")
        return 1
    print(f"OK jmcomic {importlib.metadata.version('jmcomic')} JM{found[0]} "
          f"{len(photo)} 页 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
