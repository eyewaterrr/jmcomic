# 运维手册

适用于 v3（阅读从 JM 取、服务器只缓存原图）。设计见 [architecture.md](architecture.md)。

## 1. 文件位置

| 位置 | 内容 |
| --- | --- |
| `/opt/jmcomic` | 代码（`jmshelf/`）和独立的 Python 环境（`venv/`） |
| `/etc/jmcomic/jmcomic.env` | 配置和密钥，权限 640，属主 root:jmcomic；字段说明见 `deploy/jmcomic.env.example` |
| `/var/lib/jmcomic/jmshelf.db` | 数据库 |
| `/var/lib/jmcomic/jmshelf-pre-v3.db` | 从 v1.1 升级时自动留下的数据库备份 |
| `/var/lib/jmcomic/bot.session` | Bot 登录凭证 |
| `/var/lib/jmcomic/cache/pages/` | JM 原图缓存，上限 2GB，7 天没打开的章节删除 |
| `/var/lib/jmcomic/cache/jmcovers/` | 搜索页封面缓存，最多 5000 张 |
| `/var/lib/jmcomic/covers/` | 收藏和读过的作品的封面 |
| `/var/lib/jmcomic/work/` | 上传精选时的临时目录，任务结束即删 |
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

- 把代码复制到 `/opt/jmcomic`，安装依赖，更新 systemd 单元。
- 不会覆盖已有的配置文件。
- 不会自动重启服务，所以要手动执行上面的 restart。

## 4. 从 v1.1 升级到 v3

1. 部署新代码：

   ```bash
   cd ~/projects/jmcomic && git pull
   sudo deploy/install.sh
   ```

2. 整理配置：运行 `sudo micro /etc/jmcomic/jmcomic.env`。
   - 删掉 `TG_CHANNEL_ID`（书库频道不再使用），以及 `JM_REMOTE_` 开头的几行。留着也不影响运行。
   - 如果写过 `JM_CACHE_MB=1024`，删掉这一行或改成 2048。
3. 启动服务：

   ```bash
   sudo systemctl enable --now jmcomic jmcomic-update.timer
   ```

4. 看日志确认迁移完成：运行 `journalctl -u jmcomic -n 30`。服务启动时会自动完成以下几件事，过程见 architecture.md 第 14 节：
   - 先备份数据库为 `jmshelf-pre-v3.db`，再迁移；
   - 重新导出元数据；
   - 删除旧的页面缓存。
5. 在 Bot 里发 `/status`，确认缓存和流量这两行都有数字。

**升级后不再需要的东西：**

- 书库频道里 v1.1 的帖子和 `#backup` 备份文件：留着不影响，要清理就在 Telegram 里手动删。Bot 删不了 48 小时以前的消息。
- 远程下载机：v3 不再使用。要彻底撤销，在下载机上删掉 `~/.ssh/authorized_keys` 里带 `jmshelf-worker/run.sh` 的那一行，以及 `~/jmshelf-worker` 目录。本机的 `/var/lib/jmcomic/remote_key*`、`remote_known_hosts` 也可以删掉。
- 回退：停服务，把 `jmshelf-pre-v3.db` 复制回 `jmshelf.db`，部署 v1.1 的代码（git 标签或提交 `bd67d25`），再启动。

## 5. 在新服务器上安装

1. 运行 `sudo deploy/install.sh`，它会生成 `/etc/jmcomic/jmcomic.env`。
2. 编辑这个文件，填入 Token、api_id/api_hash、精选频道 ID、所有者 ID 和阅读页网址。
3. 建 nginx 站点：
   1. 把 `deploy/nginx-jmcomic.conf` 里的域名和证书路径换成实际值；
   2. 放到 `sites-available/jmcomic`，再链接到 `sites-enabled`；
   3. 运行 `nginx -t` 检查配置，然后 reload。
4. 在 Cloudflare 新增阅读页子域名，开启代理。
5. 运行 `sudo systemctl enable --now jmcomic` 启动服务。
6. 在 BotFather 里设置 Main Mini App，步骤见第 6 节。

## 6. BotFather 设置 Main Mini App

精选频道的每条帖子都带一个“在线阅读”按钮，链接形如 `https://t.me/<bot>?startapp=a<JM号>`。要先启用 Main Mini App 才能打开阅读页：

1. 在 @BotFather 里发送 `/mybots`，选择这个 Bot。
2. 依次进入 Bot Settings → Configure Mini App → Enable Mini App。
3. 发送阅读页网址。

私聊里的“在线阅读”按钮和左下角的“书架”菜单不受影响，不设置也能用。

## 7. 运维子命令

服务运行时也能直接建上传精选任务。任务由服务里的队列执行，进度消息发到所有者的私聊；已上传的和已在队列里的自动跳过：

```bash
sudo -u jmcomic bash -c 'set -a; . /etc/jmcomic/jmcomic.env; cd /opt/jmcomic && \
  JM_DATA_DIR=/var/lib/jmcomic venv/bin/python -m jmshelf enqueue publish 123456 234567'
```

元数据的导出和恢复也用子命令，见第 8 节。

## 8. 备份与恢复

**数据库：** 服务器的整机备份包含 `jmshelf.db` 的在线快照和 `/etc/jmcomic`。服务本身不再往 Telegram 发备份。

**恢复数据库：**

1. 停止服务：`sudo systemctl stop jmcomic`。
2. 把备份里的数据库放到 `/var/lib/jmcomic/jmshelf.db`。
3. 修改属主：`sudo chown jmcomic: /var/lib/jmcomic/jmshelf.db`。
4. 启动服务：`sudo systemctl start jmcomic`。

**元数据文件：** 每本作品在 `/var/lib/jmcomic/meta/JM<号>.json` 另存一份，收藏夹变化、上传精选、补元数据时自动更新。迁移到新服务器时，把整个 `meta/` 目录（加上 `covers/`）拷过去，停服务后运行：

```bash
sudo -u jmcomic bash -c 'set -a; . /etc/jmcomic/jmcomic.env; cd /opt/jmcomic && \
  JM_DATA_DIR=/var/lib/jmcomic venv/bin/python -m jmshelf restore-meta /var/lib/jmcomic/meta'
```

`export-meta` 会从数据库重新生成全部 JSON。v1.1 格式的旧文件也能恢复。

原图缓存不用备份：丢了会在阅读时重新从 JM 取。

## 9. 撤销安装

先停掉并删除服务，再删掉 nginx 站点：

```bash
sudo systemctl disable --now jmcomic jmcomic-update.timer jmcomic-update.path
sudo rm /etc/systemd/system/jmcomic.service /etc/systemd/system/jmcomic-update.* && sudo systemctl daemon-reload
sudo rm /etc/nginx/sites-enabled/jmcomic /etc/nginx/sites-available/jmcomic && sudo systemctl reload nginx
```

数据、配置和代码分别在 `/var/lib/jmcomic`、`/etc/jmcomic`、`/opt/jmcomic`，确认不再需要后再删除。

## 10. 实测数据（2026-11-08，v3）

在本机用真实的 JM 和迁移后的数据库副本测试，Telegram 上传换成了桩：

| 项目 | 结果 |
| --- | --- |
| 打开一章（364 页，取章节详情和作品详情） | 0.29 秒 |
| 第 1 页（缓存里没有，现从 JM 取） | 0.16 秒 |
| 第 2 页（已被预读） | 0.02 秒 |
| 缓存命中 | 3 毫秒 |
| 预读 | 打开后几秒内往后 20 页都在缓存里 |
| 上传精选，13 页单章 | PDF 6MB |
| 上传精选，8 章 455 页 | 补图加生成 PDF 约 40 秒，PDF 101MB，单卷 |
| 服务器还原一页（只在生成 PDF 时） | 解码加 WebP 重新压缩约 160 毫秒；阅读时不做 |

**原图和 v1.1 的 CBZ：** 同一章 8 页抽样，v1.1 保存的 WebP（质量 90）是 JM 原图的 1.31 倍。

**同一张图 JM 每次给的字节可能不同：** CDN 有时返回不同的编码，尺寸相同，还原后平均只差约 1/255（压缩噪声）。还原算法和 jmcomic 逐像素一致，见单元测试。

## 11. 测试阅读页接口

阅读页在 Cloudflare 后面。Cloudflare 会把 Python 自带请求库（urllib）这类非浏览器请求当成爬虫，直接返回 403。

- 手动测试接口时用 curl。
- 手机里 Telegram 打开阅读页用的是正常浏览器，不受影响。

## 12. jmcomic 自动更新

**什么时候检查：**

- 每天 04:30 自动检查。
- 从 JM 下载出错时也会立即检查一次，6 小时内最多一次。

**有新版本时：** 自动完成升级。

1. 等正在执行的任务结束，停服务、升级。
2. 自检：搜索、取详情、下载两张原图并还原。
3. 自检通过就启动服务；不通过就装回原版本再启动。

**大版本：** 只私聊提醒，不自动升级。规则详见 architecture.md 第 13 节。

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

## 13. JM 收藏夹同步

1. 运行 `sudo micro /etc/jmcomic/jmcomic.env`，填写 `JM_USERNAME` 和 `JM_PASSWORD`，保存后运行 `sudo systemctl restart jmcomic`。
2. 在 Bot 里发 `/jmsync`，看过统计后点“开始同步”。同步只改“JM·”收藏夹，不下载、不生成 PDF。
3. 想每天自动同步，在 `/jmsync` 的消息里打开开关即可。
4. “反向同步到 JM”的开关也在同一条消息里，默认关闭。打开后，你在这边新收藏的作品会自动加进 JM 账号的默认收藏，只加不删。

## 14. 缓存和流量

- `/status` 显示缓存大小、缓存的章节数、这次启动以来从 JM 下载的量，以及服务的入站、出站流量（systemd IPAccounting）。
- 调整缓存：在配置里改 `JM_CACHE_MB`（上限）、`JM_CACHE_DAYS`（多少天没打开就删）、`JM_READ_AHEAD`（预读页数），然后重启服务。
- 手动清空缓存：停服务后删掉 `/var/lib/jmcomic/cache/pages/` 下的目录，再启动。
