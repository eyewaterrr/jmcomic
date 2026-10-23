"""在子进程里下载一章图片：python -m jmshelf.fetch <photo_id> <目录> <线程数>

jmcomic 是同步多线程库，峰值内存 300MB 左右，放在子进程里便于限制和隔离。
"""
import json
import sys
from pathlib import Path

import jmcomic
from jmcomic import JmImageTool

from .pack import list_images


def _save_image(cls, image, filepath):
    # jmcomic 解码打乱的图片后用 Pillow 默认参数保存，WebP 质量只有 80，这里提高到 90。
    if str(filepath).lower().endswith(".webp"):
        image.save(filepath, quality=90, method=4)
    else:
        image.save(filepath)


def main():
    photo_id, out_dir, threads = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)
    jmcomic.disable_jm_log()
    JmImageTool.save_image = classmethod(_save_image)
    option = jmcomic.JmOption.construct({
        "client": {"impl": "api"},
        "dir_rule": {"base_dir": str(out_dir), "rule": "Bd"},
        "download": {"cache": True, "threading": {"image": threads, "photo": 1}},
    })
    jmcomic.download_photo(photo_id, option)
    print(json.dumps({"pages": [p.name for p in list_images(out_dir)]}))


if __name__ == "__main__":
    main()
