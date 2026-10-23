"""在子进程里把若干 CBZ 合成 PDF：python -m jmshelf.pdf <任务描述.json>

WebP 不能直接放进 PDF，先转成 JPEG，再用 img2pdf 原样封装。img2pdf 会把整卷
图片放在内存里，所以按体积分卷，控制峰值内存。
"""
import io
import json
import sys
import zipfile
from pathlib import Path

import img2pdf
from PIL import Image

from .pack import COMICINFO, IMAGE_EXT


def main():
    spec = json.loads(Path(sys.argv[1]).read_text())
    out_dir = Path(spec["out_dir"])
    jpg_dir = out_dir / "jpg"
    jpg_dir.mkdir(parents=True, exist_ok=True)
    limit = spec["volume_bytes"]

    volumes, current, current_size, n = [], [], 0, 0
    for cbz in spec["cbz"]:
        with zipfile.ZipFile(cbz) as zf:
            names = sorted(m for m in zf.namelist() if m != COMICINFO and Path(m).suffix.lower() in IMAGE_EXT)
            for name in names:
                n += 1
                with Image.open(io.BytesIO(zf.read(name))) as im:
                    target = jpg_dir / f"{n:06d}.jpg"
                    im.convert("RGB").save(target, "JPEG", quality=spec["quality"], optimize=True)
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
