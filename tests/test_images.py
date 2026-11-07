import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from jmcomic import JmImageTool
from PIL import Image

from jmshelf.images import descramble, strips

STRIPS_JS = Path(__file__).parent.parent / "jmshelf" / "webapp" / "static" / "strips.js"
CASES = [(h, n) for h in (7, 100, 1688, 1701, 12000) for n in (0, 2, 4, 8, 10, 14, 20)]


def row_image(height: int) -> Image.Image:
    """宽 1 像素、每一行的颜色编码行号的图，还原后读颜色就知道每行来自原图哪一行。"""
    img = Image.new("RGB", (1, height))
    img.putdata([(y & 255, (y >> 8) & 255, (y >> 16) & 255) for y in range(height)])
    return img


def rows(img: Image.Image) -> list[int]:
    data = img.convert("RGB").tobytes()
    return [data[i] + (data[i + 1] << 8) + (data[i + 2] << 16) for i in range(0, len(data), 3)]


@pytest.mark.parametrize("height,num", CASES)
def test_descramble_matches_jmcomic(tmp_path, height, num):
    src = row_image(height)
    out = tmp_path / "jm.png"
    JmImageTool.decode_and_save(num, src, str(out))
    assert rows(descramble(src, num)) == rows(Image.open(out))


def test_descramble_real_sized_image_pixel_exact(tmp_path):
    random.seed(7)
    src = Image.frombytes("RGB", (64, 1688), bytes(random.getrandbits(8) for _ in range(64 * 1688 * 3)))
    out = tmp_path / "jm.png"
    JmImageTool.decode_and_save(14, src, str(out))
    assert descramble(src, 14).tobytes() == Image.open(out).convert("RGB").tobytes()


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_reader_js_uses_the_same_strips():
    """阅读页的 strips.js 和服务器的 strips() 必须完全一致。"""
    out = subprocess.check_output(["node", "-e",
        "const {strips}=require(process.argv[1]);const c=JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify(c.map(([h,n])=>strips(h,n))))", str(STRIPS_JS), json.dumps(CASES)])
    assert json.loads(out) == [[list(s) for s in strips(h, n)] for h, n in CASES]
