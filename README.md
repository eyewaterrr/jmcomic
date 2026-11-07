# JMComic · Telegram 私人漫画书库

以 Telegram 为入口的个人漫画书库：在 Bot 或 Mini App 里搜索、收藏、在线阅读，满意的作品“上传精选”成 PDF 存在 Telegram 里。

> 当前状态：v3 已实现并测试（分支 `feat/v3`），等手机验收后部署。
>
> - v3 的变化：取消入库；阅读时服务器从 JM 取原图、只做缓存，手机上用 Canvas 还原；收藏只记收藏夹；上传精选单独操作。目标是减少服务器的双向计费流量，也不再让单核 CPU 解码。
> - 设计：[docs/architecture.md](docs/architecture.md)
> - 运维（含从 v1.1 升级）：[docs/operations.md](docs/operations.md)
> - 进度：[docs/progress.md](docs/progress.md)
> - 后续计划：[docs/roadmap.md](docs/roadmap.md)
> - 服务器实测数据：[docs/environment.md](docs/environment.md)

## 代码结构

| 路径 | 内容 |
| --- | --- |
| `jmshelf/__main__.py` | 启动入口：在同一进程里运行 Telethon Bot、任务队列、原图缓存和阅读页服务；运维子命令 |
| `jmshelf/bot.py` | Bot 私聊：搜索、作品详情、收藏、上传精选、JM 收藏夹同步、任务、状态、用户管理 |
| `jmshelf/web.py` | 阅读页后端：身份校验、书架、作品、章节、原图、阅读进度、搜索 |
| `jmshelf/cache.py` | 原图缓存：按页从 JM 下载、往后预读、读者请求优先、过期和超限清理、记录每页尺寸 |
| `jmshelf/jm.py` | 调用 jmcomic：搜索、作品和章节详情（含图片地址和打乱参数）、原图、封面、JM 收藏夹 |
| `jmshelf/images.py` | 图片还原（和 jmcomic、阅读页的 `strips.js` 一致）、文件校验 |
| `jmshelf/jobs.py` | 任务队列：上传精选；后台补元数据和封面；JM 收藏夹自动同步 |
| `jmshelf/pdf.py` | 子进程：原图还原后合成 PDF，按体积分卷 |
| `jmshelf/tg.py` | 精选频道：并行分块上传、删除帖子 |
| `jmshelf/db.py` | SQLite：作品和章节元数据、收藏夹、精选记录、任务、进度；v1.1 → v3 自动迁移 |
| `jmshelf/webapp/` | Mini App 前端（`static/strips.js` 是还原算法） |
| `jmshelf/ids.py` | 从消息里识别 JM 号：链接、各种前缀、全角数字、多个号码 |
| `jmshelf/sync.py` | JM 账号收藏夹到本地收藏夹的单向镜像 |
| `jmshelf/meta.py` | 每本作品的元数据 JSON：导出、恢复 |
| `jmshelf/selftest.py` | jmcomic 自检，供自动更新使用 |
| `deploy/` | systemd 服务、jmcomic 自动更新（脚本、timer、path）、安装脚本、nginx 模板、配置示例 |
| `tests/` | 单元测试，运行 `python -m pytest` |

## 一、做什么

- **搜索：** 在 Bot 里发关键词或 JM 号；Mini App 的搜索页还能按作品、作者、标签、角色搜，排序，看排行榜和分类。
- **阅读：** 点“在线阅读”就在 Telegram 里打开 Mini App，不用先下载。默认上下滑动，可以切换成左右翻页，阅读进度保存在服务器上。
- **收藏：** 收藏夹记下想看的作品，可以自建多个收藏夹，也能同步 JM 账号的收藏夹。收藏不会触发下载。
- **上传精选：** 想长期保存、离线看的作品，生成 PDF 发到私有的精选频道，在 Telegram 里点开就能看，JM 下架了也还在。

项目只给个人或少量授权用户使用，不面向公众提供下载服务。

## 二、整体框架

```text
手机（Telegram：Bot 私聊 + Mini App 阅读页）
          │  HTTPS（Cloudflare → nginx）
   服务器上的 jmcomic 服务（单进程 + 生成 PDF 的子进程）
          │                         │
   JM 接口和图片 CDN           Telegram 精选频道（PDF）
   （阅读和精选的数据来源）
```

- **阅读：** 服务器按页从 JM 取原图（JM 的图片是打乱过的），存进有上限的缓存，原样发给手机；手机上用 Canvas 把图片还原。服务器不解码，单核 CPU 不再是瓶颈。
- **精选：** 服务器把整本还原后存成 JPEG，合成 PDF，经 MTProto 并行上传到精选频道。
- **数据：** SQLite 保存收藏夹、阅读进度、作品元数据和精选记录；漫画内容只在缓存里短期存放。

## 三、关键边界

- **依赖 JM 在线：** 没缓存、没上传精选的作品，JM 取不到时就看不了。想长期保留的就上传精选。
- **云端阅读不等于没有网络传输：** 每一页都要经服务器从 JM 中转，靠预读和缓存来弥补。
- **精选不是独立备份：** 精选频道在 Telegram 上。以后需要独立备份时，再另选外部存储。
- **访问权限：** Bot 和阅读页都校验 Telegram 用户身份，并检查白名单。本仓库是公开仓库，密钥、会话文件、数据库、频道 ID 一律不提交。只管理有权访问的内容，不绕过任何访问权限。

## 四、版本

| 版本 | 内容 |
| --- | --- |
| v1 / v1.1（2026-10-24） | 入库到 Telegram 书库频道（CBZ），从频道按页读取；收藏自动生成 PDF；JM 收藏夹同步；jmcomic 自动更新 |
| v3（2026-11-08） | 取消入库和书库频道；阅读从 JM 取、服务器只缓存原图、前端还原；收藏只记收藏夹；上传精选单独操作 |

## 七、参考项目与官方文档

以下链接按用途分类，供选型和开发参考。**列为参考不代表已经集成，也不代表这些项目与本仓库有官方合作关系。**

### 核心依赖

| 项目 | 仓库地址 | 用途 |
| --- | --- | --- |
| **JMComic-Crawler-Python** | [hect0x7/JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) | 内容获取核心（搜索、详情、图片地址和打乱参数）。直接依赖并跟踪上游更新，只用移动端 API。阅读页的还原算法和它的 `decode_and_save` 一致。 |
| **Telethon** | [codeberg.org/Lonami/Telethon](https://codeberg.org/Lonami/Telethon) | MTProto 客户端，负责 Bot 对话和精选 PDF 的大文件上传。GitHub 仓库已归档，开发迁到了 Codeberg。 |
| **img2pdf** | [josch/img2pdf](https://gitlab.mister-muffin.de/josch/img2pdf) | 把 JPEG 原样封装进 PDF，不再二次压缩。 |

### 参考与备选

| 项目 | 仓库地址 | 说明 |
| --- | --- | --- |
| **jm-view-server** | [hect0x7/jm-view-server](https://github.com/hect0x7/jm-view-server) | 参考它的阅读交互：下拉、翻页、双页、进度记忆。它面向本地文件夹，并带文件管理功能，所以不直接部署。 |
| **python-telegram-bot** | [python-telegram-bot/python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | 备选：如果更习惯它的对话写法，可以让它只负责交互，文件仍交给 Telethon。 |
| **Telegram Bot API Server** | [tdlib/telegram-bot-api](https://github.com/tdlib/telegram-bot-api) | 备选：未采用，官方 Bot API 的文件大小限制见 architecture.md 第 6 节。 |
| **JM-Cosmos II** | [GEMILUXVII/astrbot_plugin_jm_cosmos](https://github.com/GEMILUXVII/astrbot_plugin_jm_cosmos) | 参考它在聊天机器人里做搜索、打包、发送、权限管理和任务通知的方式。 |
| **JM-PDF-plugin** | [AmethystTim/JM-PDF-plugin](https://github.com/AmethystTim/JM-PDF-plugin) | 参考它的 PDF 生成、缓存和按章节下载。 |
| **jmcomic-ai** | [hect0x7/jmcomic-ai](https://github.com/hect0x7/jmcomic-ai) | 可选的 MCP / Agent Skills 扩展，不在首期范围内。 |

### 官方文档

- [JMComic-Crawler-Python 使用文档](https://jmcomic.readthedocs.io/zh-cn/latest/)：上游库的接口和配置。
- [Telegram MTProto 文件上传与下载](https://core.telegram.org/api/files)：分块上传的规则。
- [Telegram Bot API](https://core.telegram.org/bots/api)：Bot 消息、按钮和文件大小限制。
- [Telegram Mini Apps](https://core.telegram.org/bots/webapps)：Mini App 的入口、交互，以及 `initData` 身份校验。
- [Telethon 文档](https://docs.telethon.dev)：以 Bot 身份登录、发送文件。
- [Telegram Mini Apps 直达链接](https://core.telegram.org/bots/webapps#direct-link-mini-apps)：用 `startapp` 参数直接打开某一本。

**选型原则：** 优先复用成熟的上游组件。新增代码主要负责四件事：原图缓存和转发、书架和收藏夹、阅读页（含还原）、精选 PDF。不引入不必要的 AI Agent 或多平台机器人功能。
