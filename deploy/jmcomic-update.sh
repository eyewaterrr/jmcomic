#!/usr/bin/env bash
# 跟进 jmcomic 更新。由 jmcomic-update.timer 每天 04:30 运行；下载出错时服务会写
# /var/lib/jmcomic/update-request，由 jmcomic-update.path 立即触发一次。
#
# 有同一大版本内的新版本时：等正在执行的任务结束 → 停服务 → 升级 → 自检 → 启动服务；
# 自检不通过就装回原版本。大版本（如 3.x）只提醒，不自动升级。
# 结果写到 /var/lib/jmcomic/update-status.json（Bot 的 /status 会显示），有变化时私聊通知所有者。
set -uo pipefail

APP=/opt/jmcomic
DATA=/var/lib/jmcomic
PY="$APP/venv/bin/python"
PIP="$APP/venv/bin/pip"
STATUS="$DATA/update-status.json"

set -a
. /etc/jmcomic/jmcomic.env
set +a
rm -f "$DATA/update-request"

notify() {
    curl -4 -s -m 20 -o /dev/null "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_OWNER_ID}" --data-urlencode "text=$1" || true
}

record() {  # 结果 当前版本 最新版本 说明
    "$PY" - "$@" > "$STATUS.tmp" <<'EOF'
import json, sys, time
result, current, latest, message = sys.argv[1:5]
print(json.dumps({"checked_at": int(time.time()), "result": result, "current": current,
                  "latest": latest, "message": message}, ensure_ascii=False))
EOF
    mv "$STATUS.tmp" "$STATUS"
    chown jmcomic:jmcomic "$STATUS"
}

selftest() {
    runuser -u jmcomic -- bash -c "cd $APP && HOME=$DATA $PY -m jmshelf.selftest"
}

current=$("$PY" -c 'import importlib.metadata as m; print(m.version("jmcomic"))')
latest=$(curl -4 -s -m 30 https://pypi.org/pypi/jmcomic/json \
    | "$PY" -c 'import json, sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null)
if [ -z "$latest" ]; then
    record error "$current" "" "查询 PyPI 失败，明天再试"
    exit 1
fi

decision=$("$PY" - "$current" "$latest" <<'EOF'
import sys
from pip._vendor.packaging.version import Version
cur, new = Version(sys.argv[1]), Version(sys.argv[2])
print("no" if new <= cur else "major" if new.major != cur.major else "yes")
EOF
)
case "$decision" in
    no)
        record uptodate "$current" "$latest" "已是最新（$current）"
        exit 0 ;;
    major)
        record skipped "$current" "$latest" "有大版本 $latest，需要人工确认后升级"
        notify "jmcomic 发布了大版本 $latest（当前 $current），可能有不兼容的改动，没有自动升级。"
        exit 0 ;;
esac

# 等正在执行的任务结束，最多 30 分钟
for _ in $(seq 1 30); do
    running=$(sqlite3 -readonly "$DATA/jmshelf.db" "select count(*) from jobs where status='running'" 2>/dev/null || echo 0)
    [ "$running" = "0" ] && break
    sleep 60
done

systemctl stop jmcomic
if "$PIP" install -q "jmcomic==$latest" && out=$(selftest); then
    systemctl start jmcomic
    record upgraded "$latest" "$latest" "已从 $current 升级到 $latest，自检通过"
    notify "jmcomic 已自动从 $current 升级到 $latest，自检通过，服务已重启。
$out"
else
    "$PIP" install -q "jmcomic==$current"
    if selftest >/dev/null; then
        note="原版本自检正常"
    else
        note="原版本自检也没通过，可能是 JM 暂时不可用"
    fi
    systemctl start jmcomic
    record rollback "$current" "$latest" "$latest 自检未通过，已装回 $current（$note）"
    notify "jmcomic $latest 自检未通过，已装回 $current。$note。"
fi

sleep 15
if ! systemctl is-active --quiet jmcomic; then
    notify "jmcomic 服务在升级检查后没有正常启动，请用 journalctl -u jmcomic 查看日志。"
fi
