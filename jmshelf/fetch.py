"""在子进程里下载一章图片：python -m jmshelf.fetch <photo_id> <目录> <线程数>

jmcomic 是同步多线程库，峰值内存 300MB 左右，放在子进程里便于限制和隔离。
"""
import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import jmcomic
from jmcomic import JmImageTool

from .pack import list_images


def _connect_seconds(ip: str) -> float:
    started = time.monotonic()
    try:
        socket.create_connection((ip, 443), timeout=2).close()
    except OSError:
        return float("inf")
    return time.monotonic() - started


def _pin_hosts(candidates: str):
    """把 jmcomic 访问的所有域名解析到候选 IP 中连得最快的一个（环境变量 JM_PIN_IPS，逗号分隔）。

    新加坡 Oracle 按默认 DNS 访问 JM 的 Cloudflare 节点会绕到很远的地方，52 页要 80 秒；
    指定一个走本地节点的 Cloudflare IP 后只要 6 秒。只在远程下载机上开启。
    """
    from curl_cffi import CurlOpt
    from curl_cffi.requests import Session

    best = min((ip.strip() for ip in candidates.split(",") if ip.strip()), key=_connect_seconds)
    original = Session.request

    def request(self, method, url, *args, **kwargs):
        host = urlsplit(url).hostname
        if host:
            resolve = self.curl_options.setdefault(CurlOpt.RESOLVE, [])
            entry = f"{host}:443:{best}"
            if entry not in resolve:
                resolve.append(entry)
        return original(self, method, url, *args, **kwargs)

    Session.request = request
    print(f"JM 域名固定到 {best}", file=sys.stderr)


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
    if os.environ.get("JM_PIN_IPS"):
        _pin_hosts(os.environ["JM_PIN_IPS"])
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
