# 运维手册

## 1. 文件位置

| 位置 | 内容 |
| --- | --- |
| `/opt/jmcomic` | 代码（`jmshelf/`）和独立的 Python 环境（`venv/`） |
| `/etc/jmcomic/jmcomic.env` | 配置和密钥，权限 640，属主 root:jmcomic；字段说明见 `deploy/jmcomic.env.example` |
| `/var/lib/jmcomic` | 运行数据，详见下面几行 |
| `/var/lib/jmcomic/jmshelf.db` | 索引数据库 |
| `/var/lib/jmcomic/bot.session` | Bot 登录凭证 |
| `/var/lib/jmcomic/cache/` | 页面缓存，上限 1GB |
| `/var/lib/jmcomic/covers/` | 封面 |
| `/var/lib/jmcomic/work/` | 任务临时目录 |
| `/var/lib/jmcomic/meta/` | 每本作品一份元数据 JSON，用于迁移和恢复 |
| `/var/lib/jmcomic/update-status.json` | 最近一次 jmcomic 自动更新的结果 |
| `/etc/systemd/system/jmcomic.service` | 服务，以系统用户 `jmcomic` 运行 |
| `/etc/systemd/system/jmcomic-update.{service,timer,path}` | jmcomic 自动更新，脚本在 `/opt/jmcomic/bin/jmcomic-update.sh` |
| `/etc/nginx/sites-available/jmcomic` | 阅读页站点，由 `deploy/nginx-jmcomic.conf` 生成 |

## 2. 常用命令

```bash
systemctl status jmcomic              # 查看状态
journalctl -u jmcomic -f              # 实时日志
sudo systemctl restart jmcomic        # 重启
sudo micro /etc/jmcomic/jmcomic.env   # 修改配置，改完要重启
```

## 3. 更新代码

```bash
cd ~/projects/jmcomic && git pull
sudo deploy/install.sh
sudo systemctl restart jmcomic
```

`install.sh` 的行为：

- 把代码复制到 `/opt/jmcomic`，并安装依赖。
- 不会覆盖已有的配置文件。
- 不会自动重启服务，所以要手动执行上面的 restart。

## 4. 在新服务器上安装

1. 运行 `sudo deploy/install.sh`，它会生成 `/etc/jmcomic/jmcomic.env`。
2. 编辑这个文件，填入 Token、api_id/api_hash、两个频道 ID、所有者 ID 和阅读页网址。
3. 建 nginx 站点：
   1. 把 `deploy/nginx-jmcomic.conf` 里的域名和证书路径换成实际值；
   2. 放到 `sites-available/jmcomic`，再链接到 `sites-enabled`；
   3. 运行 `nginx -t` 检查配置，然后 reload。
4. 在 Cloudflare 新增阅读页子域名，开启代理。
5. 运行 `sudo systemctl enable --now jmcomic` 启动服务。
6. 在 BotFather 里设置 Main Mini App，步骤见第 5 节。

## 5. BotFather 设置 Main Mini App

书库频道和精选频道的每条帖子都带一个“在线阅读”按钮。按钮的链接形如 `https://t.me/<bot>?startapp=c<章节ID>`，要先启用 Main Mini App 才能打开阅读页。启用步骤：

1. 在 @BotFather 里发送 `/mybots`，选择这个 Bot。
2. 依次进入 Bot Settings → Configure Mini App → Enable Mini App。
3. 发送阅读页网址。

私聊里的“在线阅读”按钮和左下角的“书架”菜单不受影响，不设置也能用。

## 6. 运维子命令

服务运行时也可以用下面的命令直接建任务，任务由服务里的队列执行，进度消息会发到所有者的私聊：

```bash
sudo -u jmcomic bash -c 'set -a; . /etc/jmcomic/jmcomic.env; cd /opt/jmcomic && \
  JM_DATA_DIR=/var/lib/jmcomic venv/bin/python -m jmshelf enqueue archive 123456'
```

批量入库多个 JM 号的命令如下。它会自动去重，跳过已经入库或已在队列里的作品，所有任务共用一条进度消息；只入库，不会收藏：

```bash
sudo -u jmcomic bash -c 'set -a; . /etc/jmcomic/jmcomic.env; cd /opt/jmcomic && \
  JM_DATA_DIR=/var/lib/jmcomic venv/bin/python -m jmshelf enqueue-batch archive 123456 234567'
```

把 `archive` 换成 `favorite`，就是生成 PDF。这只生成 PDF，不会把作品放进收藏夹；要收藏，请在 Bot 或书架里操作。

元数据的导出和恢复也用子命令，见第 7 节。

## 7. 备份与恢复

**自动备份：** 服务每 24 小时生成一份数据库快照，gzip 压缩后发到书库频道，带 `#backup` 标签，只保留最近 3 份。

**恢复步骤：**

1. 停止服务：`sudo systemctl stop jmcomic`。
2. 从频道下载最新的备份。
3. 解压后放到 `/var/lib/jmcomic/jmshelf.db`。
4. 修改属主：`sudo chown jmcomic: /var/lib/jmcomic/jmshelf.db`。
5. 启动服务：`sudo systemctl start jmcomic`。

**元数据文件：** 每本作品在 `/var/lib/jmcomic/meta/JM<号>.json` 另存一份元数据，入库、生成 PDF、收藏夹变化时自动更新。

迁移到新服务器时，把整个 `meta/` 目录（加上 `covers/`）拷过去，停服务后运行：

```bash
sudo -u jmcomic bash -c 'set -a; . /etc/jmcomic/jmcomic.env; cd /opt/jmcomic && \
  JM_DATA_DIR=/var/lib/jmcomic venv/bin/python -m jmshelf restore-meta /var/lib/jmcomic/meta'
```

`export-meta` 会从数据库重新生成全部 JSON。

## 8. 撤销安装

先停掉并删除服务，再删掉 nginx 站点：

```bash
sudo systemctl disable --now jmcomic jmcomic-update.timer jmcomic-update.path
sudo rm /etc/systemd/system/jmcomic.service /etc/systemd/system/jmcomic-update.* && sudo systemctl daemon-reload
sudo rm /etc/nginx/sites-enabled/jmcomic /etc/nginx/sites-available/jmcomic && sudo systemctl reload nginx
```

数据、配置和代码分别在 `/var/lib/jmcomic`、`/etc/jmcomic`、`/opt/jmcomic`，确认不再需要后再删除。

## 9. 实测数据（2026-10-24）

两本作品的完整流程测试结果：

| 项目 | JM1485198（53 页） | JM1475699（57 页） |
| --- | --- | --- |
| 入库 | 约 60 秒（含排队） | 43 秒：排队 8 秒、下载 31 秒、打包加上传 4 秒 |
| 入库后的 CBZ | 28MB | 22.7MB |
| 收藏 | 约 6 秒 | 9 秒：取回 2 秒、转 PDF 3 秒、上传 2 秒 |
| 收藏的 PDF | 30MB | 26.2MB |
| 第 1 页（首次读取） | 0.49 秒 | 0.17 秒 |
| 已预取的页 | 0.08–0.11 秒 | 0.06–0.09 秒 |
| 跳读未预取的页 | 0.17 秒 | 0.16–0.30 秒 |

**速度瓶颈：** 下载并解码约 0.5 秒/页。单核 CPU 解码是瓶颈，多开线程也不会更快。

**内存：** 常驻约 90MB；入库、收藏时整个服务的峰值 255–374MB（含子进程）；上限设为 900MB。

**图片质量：** 按 WebP 质量 90 保存。

## 10. 测试阅读页接口

阅读页在 Cloudflare 后面。Cloudflare 会把 Python 自带请求库（urllib）这类非浏览器请求当成爬虫，直接返回 403。

- 手动测试接口时用 curl。
- 手机里 Telegram 打开阅读页用的是正常浏览器，不受影响。

## 11. jmcomic 自动更新

**什么时候检查：**

- 每天 04:30 自动检查。
- 下载出错时也会立即检查一次，6 小时内最多一次。

**有新版本时：** 自动完成升级。

1. 停服务、升级。
2. 自检，内容包括搜索、取详情、下载并解码两张图片。
3. 自检通过就启动服务；不通过就装回原版本再启动。

**大版本：** 只私聊提醒，不自动升级。规则详见 architecture.md 第 12 节。

```bash
sudo systemctl start jmcomic-update                # 立即检查一次
cat /var/lib/jmcomic/update-status.json            # 最近一次的结果（Bot 的 /status 也会显示）
journalctl -u jmcomic-update -n 50                 # 更新日志
systemctl list-timers jmcomic-update.timer         # 下次检查时间
sudo -u jmcomic bash -c 'cd /opt/jmcomic && venv/bin/python -m jmshelf.selftest'   # 手动自检
```

**暂停自动更新：**

```bash
sudo systemctl disable --now jmcomic-update.timer jmcomic-update.path
```

## 12. JM 收藏夹同步

1. 运行 `sudo micro /etc/jmcomic/jmcomic.env`，填写 `JM_USERNAME` 和 `JM_PASSWORD`，保存后运行 `sudo systemctl restart jmcomic`。
2. 在 Bot 里发 `/jmsync`，看过统计后点“开始同步”。
3. 想每天自动同步，在 `/jmsync` 的消息里打开开关即可。
