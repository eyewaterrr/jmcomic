"""JM 图片还原和文件工具。

JM 的图片按行切成若干条后倒序排列（“打乱”）。切几条由 jmcomic 的 JmImageTool.get_num_by_detail 算出，
随章节信息一起发给阅读页；阅读页用 Canvas 按 webapp/static/strips.js 还原，服务器不解码。
服务器只在生成精选 PDF 时用这里的 descramble 还原。两边的算法和 jmcomic 的 decode_and_save 逐行一致。
"""
import hashlib
from pathlib import Path

from PIL import Image

IMAGE_EXT = {".webp", ".jpg", ".jpeg", ".png", ".gif"}


def strips(height: int, num: int) -> list[tuple[int, int, int]]:
    """[(源 y, 目标 y, 高度), ...]：原图从下往上切成 num 条，依次从上往下摆。num 为 0 表示没打乱。"""
    if not num:
        return [(0, 0, height)]
    move, over = divmod(height, num)
    out = []
    for i in range(num):
        size, sy, dy = move, height - move * (i + 1) - over, move * i
        if i == 0:
            size += over
        else:
            dy += over
        out.append((sy, dy, size))
    return out


def descramble(img: Image.Image, num: int) -> Image.Image:
    if not num:
        return img
    width, height = img.size
    out = Image.new("RGB", (width, height))
    for sy, dy, size in strips(height, num):
        out.paste(img.crop((0, sy, width, sy + size)), (0, dy))
    return out


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
