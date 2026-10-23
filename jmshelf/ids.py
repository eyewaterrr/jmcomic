"""从消息文字里识别 JM 号。

能识别：
- 链接：任意镜像域名下的 /album/<号> 或 /photo/<号>
- 带前缀：JM123456、jm 123456、JM-123456、#JM123456、禁漫123456、车号：123456
- 全角字符：ＪＭ１２３４５６
- 只有数字的消息：123456，或用空格、逗号、换行隔开的多个号码

其他文字一律当作搜索关键词。
"""
import re
import unicodedata

MAX_IDS = 30
URL = re.compile(r"(?:https?://)?[^\s/]+\.[^\s/]+(?:/[^\s/]*)*?/(album|photo)/(\d{2,9})(?!\d)", re.IGNORECASE)
PREFIXED = re.compile(r"(?<![a-z0-9])(?:jm|禁漫|车号|本号)\s*[:：#号\-_]?\s*(\d{2,9})(?!\d)", re.IGNORECASE)
NUMBERS_ONLY = re.compile(r"^[\d\s,，、;；/|.。]+$")
BARE = re.compile(r"\d{3,9}")


def extract(text: str) -> list[tuple[str, int]]:
    """返回 [(类型, 号码)]，类型是 album 或 photo；按出现顺序去重。"""
    text = unicodedata.normalize("NFKC", text or "")
    found: list[tuple[str, int]] = []

    def add(kind: str, number: str):
        item = (kind, int(number))
        if item not in found and len(found) < MAX_IDS:
            found.append(item)

    for m in URL.finditer(text):
        add(m.group(1).lower(), m.group(2))
    rest = URL.sub(" ", text)
    for m in PREFIXED.finditer(rest):
        add("album", m.group(1))
    if not found and NUMBERS_ONLY.match(rest.strip() or "x"):
        for m in BARE.finditer(rest):
            add("album", m.group(0))
    return found
