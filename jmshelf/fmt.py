"""Telegram 消息格式工具。

约定：不用表情符号；表格放在 <code> 里，表头 + 虚线，列间两个空格，
数字右对齐，表格内只用 ASCII，中文说明写在表格外。
"""
import html


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def clip(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def text_table(headers: list[str], rows: list[list], right: set[int] = frozenset()) -> str:
    cells = [[str(c) for c in headers]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]

    def line(row):
        out = [c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(row, widths))]
        return "  ".join(out).rstrip()

    body = [line(cells[0]), "  ".join("-" * w for w in widths)] + [line(r) for r in cells[1:]]
    return "<code>" + esc("\n".join(body)) + "</code>"


def size_text(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"
