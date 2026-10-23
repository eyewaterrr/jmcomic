"""CBZ 打包：ZIP 不压缩（STORED），每页在文件里是连续的一段，记录偏移以便按页读取。"""
import hashlib
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image

IMAGE_EXT = {".webp", ".jpg", ".jpeg", ".png", ".gif"}
COMICINFO = "ComicInfo.xml"


@dataclass
class Page:
    name: str
    offset: int
    size: int
    width: int
    height: int


def list_images(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT)


def comicinfo_xml(album: dict, chapter: dict, page_count: int) -> str:
    root = ET.Element("ComicInfo")
    fields = {
        "Title": chapter["title"] if len(album["chapters"]) > 1 else album["title"],
        "Series": album["title"],
        "Number": str(chapter["sort"]),
        "Count": str(len(album["chapters"])),
        "Writer": ", ".join(album["authors"]),
        "Genre": ", ".join(album["works"]),
        "Tags": ", ".join(album["tags"]),
        "PageCount": str(page_count),
        "Web": f"https://18comic.vip/album/{album['jm_id']}",
        "LanguageISO": "zh",
        "Notes": f"jm={album['jm_id']} photo={chapter['photo_id']}",
    }
    for key, value in fields.items():
        if value:
            ET.SubElement(root, key).text = value
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def member_offsets(path: Path) -> dict[str, tuple[int, int]]:
    """读取每个成员数据在 ZIP 文件中的起始偏移和长度。"""
    result = {}
    with zipfile.ZipFile(path) as zf, open(path, "rb") as f:
        for info in zf.infolist():
            if info.compress_type != zipfile.ZIP_STORED:
                raise ValueError(f"{info.filename} 不是 STORED，无法按页读取")
            f.seek(info.header_offset + 26)
            name_len, extra_len = struct.unpack("<HH", f.read(4))
            result[info.filename] = (info.header_offset + 30 + name_len + extra_len, info.file_size)
    return result


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build_cbz(images: list[Path], out: Path, comicinfo: str) -> tuple[list[Page], str]:
    names = [f"{i:04d}{p.suffix.lower()}" for i, p in enumerate(images, 1)]
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, src in zip(names, images):
            zf.write(src, name)
        zf.writestr(COMICINFO, comicinfo)
    offsets = member_offsets(out)
    pages = []
    for name, src in zip(names, images):
        with Image.open(src) as im:
            width, height = im.size
        offset, size = offsets[name]
        pages.append(Page(name, offset, size, width, height))
    return pages, file_sha256(out)
