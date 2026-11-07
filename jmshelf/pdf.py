"""在子进程里把一本作品的原图合成 PDF：python -m jmshelf.pdf <任务描述.json>

输入是缓存里的 JM 原图和每页的打乱参数 num：还原后直接存成 JPEG（不再先存 WebP 再转，少压缩一次），
再用 img2pdf 原样封装。img2pdf 会把整卷图片放在内存里，所以按体积分卷，控制峰值内存。
子进程以较低优先级运行，免得拖慢 Bot 和阅读页。
"""
import json
import os
import sys
from pathlib import Path

import img2pdf
from PIL import Image

from .images import descramble


def main():
    try:
        os.nice(10)
    except OSError:
        pass
    spec = json.loads(Path(sys.argv[1]).read_text())
    out_dir = Path(spec["out_dir"])
    jpg_dir = out_dir / "jpg"
    jpg_dir.mkdir(parents=True, exist_ok=True)
    limit = spec["volume_bytes"]

    volumes, current, current_size = [], [], 0
    for n, (path, num) in enumerate(spec["pages"], 1):
        with Image.open(path) as im:
            im.load()
            page = descramble(im.convert("RGB"), num)
        target = jpg_dir / f"{n:06d}.jpg"
        page.save(target, "JPEG", quality=spec["quality"], optimize=True)
        size = target.stat().st_size
        if current and current_size + size > limit:
            volumes.append(current)
            current, current_size = [], 0
        current.append(str(target))
        current_size += size
    if current:
        volumes.append(current)

    outputs = []
    for i, files in enumerate(volumes, 1):
        target = out_dir / (f"{spec['name']}.pdf" if len(volumes) == 1 else f"{spec['name']} 卷{i}.pdf")
        with open(target, "wb") as f:
            img2pdf.convert(files, outputstream=f, title=spec["title"], author=spec["author"],
                            subject=f"jm={spec['jm_id']} vol={i}/{len(volumes)} pages={len(files)}",
                            creator="jmshelf", producer="img2pdf")
        for p in files:
            Path(p).unlink()
        outputs.append({"path": str(target), "pages": len(files)})
    print(json.dumps({"volumes": outputs}))


if __name__ == "__main__":
    main()
