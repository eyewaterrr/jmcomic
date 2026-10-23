#!/usr/bin/env bash
# 安装或更新服务：sudo deploy/install.sh
# 代码复制到 /opt/jmcomic，数据在 /var/lib/jmcomic，配置在 /etc/jmcomic/jmcomic.env。
# 不会覆盖已有的配置文件；不会自动重启服务。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/jmcomic
ETC=/etc/jmcomic
DATA=/var/lib/jmcomic

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo 运行" >&2
    exit 1
fi

id jmcomic >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin jmcomic
install -d -m 755 "$APP"
install -d -o root -g jmcomic -m 750 "$ETC"
install -d -o jmcomic -g jmcomic -m 750 "$DATA"

rm -rf "$APP/jmshelf.new"
cp -r "$REPO/jmshelf" "$APP/jmshelf.new"
find "$APP/jmshelf.new" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$APP/jmshelf.new"
chmod -R u=rwX,go=rX "$APP/jmshelf.new"
rm -rf "$APP/jmshelf"
mv "$APP/jmshelf.new" "$APP/jmshelf"
install -m 644 "$REPO/requirements.txt" "$APP/requirements.txt"

[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

if [ ! -f "$ETC/jmcomic.env" ]; then
    install -o root -g jmcomic -m 640 "$REPO/deploy/jmcomic.env.example" "$ETC/jmcomic.env"
    echo "已生成 $ETC/jmcomic.env，请填写后再启动：sudo micro $ETC/jmcomic.env"
fi

install -m 644 "$REPO/deploy/jmcomic.service" /etc/systemd/system/jmcomic.service
systemctl daemon-reload
echo "安装完成。启动或重启：sudo systemctl restart jmcomic；查看日志：journalctl -u jmcomic -f"
