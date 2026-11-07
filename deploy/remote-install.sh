#!/usr/bin/env bash
# 安装或更新远程下载机：sudo deploy/remote-install.sh [管理用 SSH 目标，默认 oracle-sg]
#
# 管理用 SSH 目标是 root 已能登录下载机的别名，只在安装时使用。脚本会：
# 1. 给 jmcomic 用户生成专用密钥 /var/lib/jmcomic/remote_key（已有就沿用）
# 2. 把下载、打包用到的文件放到下载机的 ~/jmshelf-worker，建 Python 环境，jmcomic、Pillow 版本与本机一致
# 3. 把这把密钥加进下载机的 authorized_keys，用强制命令限定它只能运行 run.sh
# 4. 经已信任的管理连接取下载机的主机公钥，写到 /var/lib/jmcomic/remote_known_hosts
# 不修改 /etc/jmcomic/jmcomic.env，最后打印需要填写的配置。改了 fetch.py、pack.py、remote.py 后重新运行。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ADMIN="${1:-oracle-sg}"
APP=/opt/jmcomic
DATA=/var/lib/jmcomic
KEY="$DATA/remote_key"
UV_VERSION=0.13.5

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo 运行" >&2
    exit 1
fi

HOST=$(ssh -G "$ADMIN" | awk '$1 == "hostname" {print $2}')
PORT=$(ssh -G "$ADMIN" | awk '$1 == "port" {print $2}')
LOGIN=$(ssh -G "$ADMIN" | awk '$1 == "user" {print $2}')
version() { "$APP/venv/bin/python" -c "import importlib.metadata as m; print(m.version('$1'))"; }
JMCOMIC=$(version jmcomic)
PILLOW=$(version pillow)

[ -f "$KEY" ] || runuser -u jmcomic -- ssh-keygen -q -t ed25519 -N "" -C "jmcomic@$(hostname)" -f "$KEY"
PUB=$(cut -d' ' -f1,2 "$KEY.pub")

echo "上传代码到 $ADMIN:~/jmshelf-worker"
tar -C "$REPO" -cf - jmshelf/__init__.py jmshelf/fetch.py jmshelf/pack.py jmshelf/remote.py deploy/remote-run.sh \
    | ssh "$ADMIN" 'mkdir -p ~/jmshelf-worker && tar -C ~/jmshelf-worker -xf - \
        && mv ~/jmshelf-worker/deploy/remote-run.sh ~/jmshelf-worker/run.sh && rmdir ~/jmshelf-worker/deploy \
        && chmod 755 ~/jmshelf-worker/run.sh'

echo "安装 Python 环境（jmcomic $JMCOMIC，Pillow $PILLOW）"
ssh "$ADMIN" "bash -s -- '$UV_VERSION' '$JMCOMIC' '$PILLOW' '$PUB jmcomic@$(hostname)'" <<'EOF'
set -euo pipefail
W="$HOME/jmshelf-worker"
cd "$W"
export UV_CACHE_DIR="$W/.uvcache"
if [ ! -x bin/uv ]; then
    name="uv-$(uname -m)-unknown-linux-gnu"
    url="https://github.com/astral-sh/uv/releases/download/$1/$name.tar.gz"
    tmp=$(mktemp -d)
    curl -sSfL -o "$tmp/$name.tar.gz" "$url"
    curl -sSfL -o "$tmp/$name.tar.gz.sha256" "$url.sha256"
    (cd "$tmp" && sha256sum -c --quiet "$name.tar.gz.sha256" && tar xzf "$name.tar.gz")
    install -D -m 755 "$tmp/$name/uv" bin/uv
    rm -rf "$tmp"
fi
[ -x venv/bin/python ] || bin/uv venv -q -p python3 venv
bin/uv pip install -q -p venv/bin/python "jmcomic==$2" "pillow==$3"

# 同一把密钥只保留一行，强制命令限定只能运行 run.sh
blob=$(echo "$4" | cut -d' ' -f2)
mkdir -p -m 700 ~/.ssh
touch ~/.ssh/authorized_keys
{ grep -vF "$blob" ~/.ssh/authorized_keys || true; echo "command=\"$W/run.sh\",restrict $4"; } > ~/.ssh/authorized_keys.new
chmod 600 ~/.ssh/authorized_keys.new
mv ~/.ssh/authorized_keys.new ~/.ssh/authorized_keys
EOF

echo "记录下载机主机公钥"
hostkey=$(ssh "$ADMIN" 'cat /etc/ssh/ssh_host_ed25519_key.pub' | cut -d' ' -f1,2)
target="$HOST"
[ "$PORT" = 22 ] || target="[$HOST]:$PORT"
echo "$target $hostkey" > "$DATA/remote_known_hosts"
chown jmcomic:jmcomic "$DATA/remote_known_hosts"
chmod 644 "$DATA/remote_known_hosts"

echo "检查 jmcomic 用户的连接"
runuser -u jmcomic -- ssh -F none -p "$PORT" -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$DATA/remote_known_hosts" "$LOGIN@$HOST" check

cat <<EOF
安装完成。在 /etc/jmcomic/jmcomic.env 里填写（已填过就不用改），然后 sudo systemctl restart jmcomic：
JM_REMOTE_HOST=$LOGIN@$HOST
JM_REMOTE_PORT=$PORT
EOF
