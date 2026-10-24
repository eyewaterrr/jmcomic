#!/bin/sh
# 远程下载机上 authorized_keys 的强制命令，由 deploy/remote-install.sh 安装到 ~/jmshelf-worker/run.sh。
# 本机 jmcomic 服务经 SSH 调用：ssh <下载机> <photo_id> <线程数> <jmcomic 版本>，或 ssh <下载机> check
# 参数在 jmshelf/remote.py 里校验，这把密钥只能运行它。
cd "$(dirname "$0")" || exit 1
export PATH="$PWD/bin:$PATH" PYTHONPATH="$PWD" UV_CACHE_DIR="$PWD/.uvcache"
# 新加坡 Oracle 按默认 DNS 访问 JM 的 Cloudflare 节点很慢，固定到走本地节点的 IP（见 jmshelf/fetch.py）
export JM_PIN_IPS="104.17.1.1,104.18.1.1,104.24.1.1,104.25.1.1"
exec nice -n 10 venv/bin/python -m jmshelf.remote
