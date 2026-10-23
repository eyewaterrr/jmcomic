# 架构设计（v2 建议稿）

本文根据目标服务器的实测结果（见 [environment.md](environment.md)）修订 README 初版规划。标“待确认”的地方需要项目负责人决定，汇总在 [progress.md](progress.md)。

## 1. 与初版规划的主要差异

| 项目 | 初版 | 建议 | 原因 |
| --- | --- | --- | --- |
| 频道文件读写 | Bot API（HTTP），评估自建 Bot API Server | Bot 账号直接走 MTProto（Telethon） | 官方 Bot API 上传最多 50MB、下载最多 20MB，长篇必然超限。自建 Bot API Server 要编译 TDLib，读文件时也要先把整个文件落盘，本机只剩约 5GB 磁盘。MTProto 下 Bot 能上传 2000MB，并能从指定偏移开始下载。 |
| 云端阅读 | 未缓存时先取回整本再解压 | 按页从频道读取字节区间 | CBZ 不压缩打包时，每页在文件里是连续的一段。入库时记下偏移，阅读时只取这一页。首次打开不用等整本下载，也不需要大缓存。 |
| 阅读器 | 复用或改造 jm-view-server | 自研轻量阅读页（Telegram Mini App） | jm-view-server 面向本地文件夹和局域网，带删除、重命名等文件管理功能。要接 Telegram 存储，得先把整本解压到本地。它的阅读交互可以参考。 |
| Bot 框架 | python-telegram-bot | Telethon 同时负责对话和文件 | 存储层本来就要用 MTProto，这样只需一个库、一个连接。Telethon 默认只走 IPv4，正好避开本机 IPv6 到阿姆斯特丹的故障。 |
| 内容获取 | jmcomic | jmcomic，只用 API 端，图片线程 2–4 | 本机访问网页端返回 403，API 端正常。1 vCPU 下解码是瓶颈，8 线程不比 4 线程快，内存却多用近一倍。 |
| Google Drive | google-api-python-client | rclone（只申请 `drive.file` 权限）+ 专用 Google 账号 | rclone 自带断点续传和令牌刷新。个人 Gmail 不能用服务账号（服务账号没有存储配额）。 |
| 部署 | VPS | VPS 上一个 systemd 服务加若干子进程；对外复用现有 nginx 和 Cloudflare 源站证书 | 只有一个用户，单进程最简单；本机已有“只允许 Cloudflare 回源”的配置。 |

## 2. 总体结构

```text
安卓 Telegram
  ├─ 与 Bot 对话：搜索、入库、收藏、任务进度
  └─ Mini App 阅读页 https://jm.<域名>        子域名待确认
            │ HTTPS，经 Cloudflare 代理
       nginx：源站证书，只放行 Cloudflare 回源
            │ 127.0.0.1
┌──────────── jmcomic 服务（单进程 asyncio，systemd）────────────┐
│ Telethon Bot 会话（MTProto，IPv4）   FastAPI（阅读接口 + 静态页） │
│ 任务队列（SQLite 持久化，串行执行）  页面读取（区间下载 + 小缓存）│
└──────┬─────────────────────┬─────────────────────────┬────────┘
       │ 子进程              │ MTProto                 │ 子进程
  jmcomic 下载/解码     Telegram 私有频道            rclone → Google Drive
  （API 端）            CBZ 归档 + 每日数据库备份      （精选收藏）
```

**为什么只用一个进程：**

- 只服务一个人。
- Telethon 的会话文件同一时间只能被一个进程使用。

**为什么下载放进子进程：** jmcomic 是同步的多线程库，实测峰值内存 300–530MB。放进子进程可以单独限制内存，崩溃时也不会拖垮 Bot。

## 3. 存储格式

- 一章（JM 的 photo）一个 CBZ。只有一章的作品就是一本一个文件。
- 用 `zipfile.ZIP_STORED` 打包，不压缩。WebP 和 JPEG 本身已经压缩过，再压也省不了多少；不压缩时每页数据是连续的，可以按字节区间单独读取。
- 文件名依次为 `0001.webp`、`0002.webp`……，另附 `ComicInfo.xml`，写入标题、作者、标签、页数和来源链接。这是 Mihon、Komga、Kavita 等阅读器通用的元数据格式。
- 入库时把每页的数据偏移和长度写入 SQLite 的 `pages` 表。
- 单个文件最大 2000MB（Bot 通过 MTProto 上传的上限），超过就分卷，这种情况很少见。
- CBZ 以文件消息发到频道，附 320px 封面作缩略图。消息说明的格式如下：

```text
[JM1485198] 标题
#JM1485198
meta: jm=1485198 photo=1485198 ep=1/1 pages=53 sha256=ab12... v=1
```

第一行给人看；`meta:` 行给程序解析。数据库丢失时，可以靠 `meta:` 行从频道重建索引（见第 7 节）。

## 4. 阅读流程

1. Bot 发出 `web_app` 类型的“在线阅读”按钮，用户点开 Mini App。
2. 页面把 Telegram 提供的 `initData` 发给服务器。服务器依次检查：
   - 用 Bot Token 计算的 HMAC 签名正确；
   - `auth_date` 没有过期；
   - 用户 ID 在白名单里。
   通过后，服务器发一个 HttpOnly 会话 Cookie，建议有效期 12 小时。之后的 `<img>` 请求都靠这个 Cookie 鉴权。
3. 图片请求 `GET /p/{photo_id}/{n}` 的处理顺序：
   1. 先查本地页面缓存。缓存放在磁盘上，按最近最少使用淘汰，上限建议 1GB。
   2. 没命中，就按 `pages` 表里的偏移，用 Telethon 从频道消息下载这一段字节，写入缓存后返回。
   3. 同时在后台预取后面 5–10 页。
4. 页面滚动时，按一定间隔上报 `(用户, 章节, 页码)`。下次打开从这一页继续。
5. 响应头设为 `Cache-Control: private, max-age=...`：手机 WebView 会缓存图片，Cloudflare 不会缓存。

**补充说明：**

- **文件引用过期：** 频道消息里的文件引用（file_reference）会过期。按 `(频道, 消息 ID)` 重新获取一次消息就能刷新。进程里可以把消息对象缓存 1 小时左右。
- **预期延迟：** 读一页约需一个 MTProto 往返加传输时间。本机到阿姆斯特丹约 150ms，到迈阿密约 72ms，到新加坡约 170ms，实际取决于 Bot 所在的 DC。具体数值在阶段 0 实测。

## 5. 入库流程

任务状态依次为 `queued`、`fetching`、`packing`、`uploading`、`done`。出错时进入 `failed` 并记录原因，最多自动重试 3 次，间隔逐次加长。

1. **去重：** 如果同一个 photo_id 已经有频道消息 ID，就直接返回已有记录。
2. **检查磁盘：** 剩余空间不够“预计大小 × 2 + 1GB”时，拒绝开始并提示用户。
3. **下载：** 在子进程里运行 jmcomic，下载到 `work/{job_id}/`。配置为 `client.impl: api`、`download.threading.image: 4`。
4. **打包：** 生成 CBZ，写入 `ComicInfo.xml`，记录每页偏移，计算 sha256。
5. **上传：** 用 Telethon 上传到频道。上传过程中大约每 5 秒更新一次 Bot 消息里的进度。
6. **收尾：** 在一个事务里写入数据库，然后删除工作目录。前几页可以留在页面缓存里，方便马上阅读。

**调度规则：**

- 同一时间只跑一个任务，因为服务器只有 1 个 vCPU。
- 服务重启后，停在 `fetching`、`packing`、`uploading` 的任务会退回 `queued` 重新执行。上传前会先查去重，所以不会重复上传。

## 6. Google Drive 收藏

**工具：** 推荐用 rclone，而不是自己调用 Drive API。rclone 自带断点续传、分块上传和令牌刷新，配置好之后上传只需要一条命令：

```bash
rclone copyto <本地文件> gdrive:JMComic/<文件名>
```

**授权：**

- 在 Google Cloud 创建自己的 OAuth 客户端，类型选“桌面应用”。
- 权限只申请 `drive.file`，也就是只能访问本程序自己创建的文件。
- 把应用的发布状态设为“正式”。处于“测试”状态时，刷新令牌 7 天后就会失效。`drive.file` 属于非敏感权限，正式发布不需要 Google 审核。
- 不要用服务账号：服务账号没有存储配额，个人 Gmail 用不了。

**流程：**

1. 从频道把整个 CBZ 下载到临时目录。
2. 按需转换成 PDF（可选）。
3. 用 rclone 上传。
4. 记录 Drive 文件 ID，然后删除临时文件。

**格式（待确认）：**

- CBZ：保留原图，但需要用漫画阅读器 App 打开。
- PDF：可以在 Drive 里直接预览，但要先把 WebP 转成 JPEG，体积和画质都会变化。

**账号（待确认）：** 建议用一个专门的 Google 账号。Google 会自动扫描网盘内容，某些成人漫画题材可能被判违规，一旦被封，整个 Google 账号（包括 Gmail 等）都会受影响。如果只想备份、不需要在 Drive 里直接看，也可以用 rclone crypt 加密后再上传。

## 7. 数据与备份

SQLite 使用 WAL 模式，表结构如下：

| 表 | 主要字段 |
| --- | --- |
| `albums` | jm_id、标题、作者、标签、章节数、页数、入库时间 |
| `chapters` | photo_id、jm_id、序号、标题、页数、频道消息 ID、文件大小、sha256 |
| `pages` | photo_id、页序号、文件名、数据偏移、长度 |
| `jobs` | id、类型（archive / drive）、目标、状态、尝试次数、错误、进度消息、时间戳 |
| `progress` | 用户 ID、jm_id、photo_id、页码、更新时间 |
| `favorites` | jm_id / photo_id、格式、Drive 文件 ID、上传时间 |

**备份：** 每天用 `sqlite3 .backup` 生成一份一致的数据库快照，压缩后作为文件发到同一个私有频道，只保留最近若干份。

**重建：**

1. Telegram 不允许 Bot 翻阅频道历史，但允许 Bot 按消息 ID 批量取消息（每次 100 条）。所以从 1 号消息扫到最新 ID，解析说明里的 `meta:` 行，就能恢复 `albums` 和 `chapters`。
2. `pages` 表的偏移不用下载整本：只下载 CBZ 末尾的 ZIP 中央目录，就能重新算出来。

## 8. 安全

- **密钥不进仓库：** 本仓库是公开的。Token、api_hash、频道 ID、会话文件、数据库、Cookie 密钥和 rclone 配置一律不提交，放在服务器的 `/etc/jmcomic/`（600 权限），或者用 systemd 的 `EnvironmentFile` 加载。
- **白名单：** 配置文件里列出允许使用的 Telegram 用户 ID，Bot 不回应其他人。
- **Web 访问：**
  - 服务只监听 127.0.0.1；nginx 沿用现有规则，只放行 Cloudflare 回源。
  - 所有接口都要求会话 Cookie。
  - 图片接口按 photo_id 和页码访问，不暴露服务器上的文件路径。
- **会话文件：** Telethon 的会话文件相当于 Bot 的登录凭证，要设为 600 权限。丢了可以用 Token 重新登录。
- **频道：** 保持私有，不生成邀请链接。
- **服务权限：** 用专用的系统用户运行服务。systemd 里加上 `NoNewPrivileges`、`ProtectSystem=strict`，并用 `MemoryMax` 限制内存。

## 9. 资源预算

本机有 1 vCPU、1.9GB 内存，剩余磁盘约 5.1GB。

| 项目 | 预算 |
| --- | --- |
| 主服务常驻内存 | 约 100–150MB（估算，阶段 0 实测） |
| 下载子进程 | 4 线程实测峰值 294MB；systemd 限制 600MB |
| 页面缓存 | 1GB，最近最少使用的先淘汰 |
| 工作目录 | 同一时间一个任务，通常小于 500MB，任务完成即删 |
| 磁盘保底 | 剩余空间低于 1.5GB 时暂停接新任务 |

## 10. Bot 交互与消息格式

**命令：**

- `/search 关键词`：返回前若干条结果。未入库的带“入库”按钮，已入库的带“阅读”按钮。
- 直接发 JM 号（纯数字或 `JM123456`）：显示作品详情，以及入库、阅读、收藏按钮。
- `/library`：打开 Mini App 书架。
- `/jobs`：查看当前任务队列。

**消息格式**（与本人其他 Bot 保持一致）：

- 不使用表情符号。
- 表格放在 `<code>` 里：表头下面一行虚线，列之间空两格，数字右对齐。
- 表格里只用 ASCII 字符，宽度控制在 40 个字符左右；中文标题和说明写在表格外。

## 11. 部署

| 项目 | 做法 |
| --- | --- |
| 代码 | 开发目录 `~/projects/jmcomic`，部署到 `/opt/jmcomic`，用独立的 venv |
| 依赖 | telethon、jmcomic、fastapi、uvicorn；rclone 用官方二进制 |
| systemd | `jmcomic.service` 运行主服务；每日备份可以在主服务里定时执行 |
| nginx | 新建一个站点，参照现有站点的写法：源站证书 + 只放行 Cloudflare 回源 |
| DNS | 在 Cloudflare 新增子域名，开启代理 |

**注意：** Mini App 页面走的是手机的系统网络，不经过 Telegram 内置的代理。如果手机所在网络需要代理才能访问该域名，要在系统层面（VPN）处理。
