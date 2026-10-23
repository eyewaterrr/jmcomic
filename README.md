# JMComic · Telegram 私人漫画书库

以 Telegram 为统一入口，结合服务器端内容获取、在线阅读和 PDF 精选收藏，构建个人使用的云端漫画书库。

> 当前状态：架构规划阶段，尚无代码。已在目标服务器上做过可行性实测（[docs/environment.md](docs/environment.md)），并据此修订了存储和阅读方案（[docs/architecture.md](docs/architecture.md)）；待确认事项和下一步见 [docs/progress.md](docs/progress.md)。本文描述的功能都还没有实现或部署。

## 一、最终目的

在 Telegram 中完成“查找 → 入库 → 在线阅读 → 收藏”的完整流程：

- 漫画主要归档在私人 Telegram 频道。
- 安卓手机不用先下载整本，就能按页在线阅读。
- 满意的作品可以一键转成 PDF，放进另一个私有的“精选”频道。

服务器负责内容处理和少量缓存，不长期保存全部漫画。项目只给个人或少量授权用户使用，不面向公众提供下载服务。

## 二、整体框架

```text
Telegram 安卓 / 桌面客户端
          │
          ├── Bot：搜索、入库、任务进度、收藏操作
          └── Mini App 阅读页：书架、按页阅读、进度
                         │  HTTPS（Cloudflare → nginx）
                VPS 应用服务（单进程 + 子进程）
                         │
          ┌──────────────┼──────────────────┐
          │              │                  │
      内容获取        书库频道（私有）      精选频道（私有）
  jmcomic（API 端）   CBZ 归档              收藏 PDF
                         │   两个频道都通过 MTProto 读写
          SQLite 索引（含每页偏移）+ 小容量页面缓存
```

图中只表示模块分工，手机不会直接连接存储。文件传输、权限校验和任务调度都由服务器统一处理。

## 三、开发架构

采用“独立业务应用 + 上游能力复用”的方式，部署在一台 Ubuntu VPS 上（1 vCPU / 2GB 内存 / 20GB 磁盘）。保持模块化，不引入微服务。

| 模块 | 主要职责 |
| --- | --- |
| Telegram 交互层 | Telethon 以 Bot 身份直连 MTProto，处理搜索、入库、收藏和任务通知；通过按钮打开 Mini App 阅读页。只响应白名单用户，所有者可以在 Bot 里增删用户。 |
| 书库与任务服务 | 管理作品、章节、归档状态和收藏状态；任务队列存在 SQLite 里，串行执行，支持去重、重试和重启后续跑。 |
| 内容获取层 | 直接依赖 `jmcomic`（JMComic-Crawler-Python），只用移动端 API；在子进程中运行，限制线程数和内存。 |
| Telegram 存储层 | 用同一个 Telethon 会话把 CBZ 上传到私有频道（单文件最大 2000MB），也能只读取其中某一页的字节。 |
| 阅读服务 | FastAPI 提供阅读页和图片接口。校验 Mini App 身份后，按索引里的偏移从频道读取单页，并做预取和磁盘缓存。默认上下滑动阅读，可以切换成左右翻页。 |
| 收藏层 | 把作品转成 PDF，发到第二个私有频道（精选频道），并记录消息 ID，避免重复上传。 |
| 数据与缓存层 | SQLite（WAL 模式）保存书目、每页偏移、任务和阅读进度；数据库每天备份到频道；页面缓存有上限（建议 1GB）。 |

归档格式定为 CBZ：ZIP 不压缩，附 `ComicInfo.xml`，一章一个文件；多章节作品默认把所有章节一起入库。收藏一律存为 PDF。

**频道与私聊的分工：**

- 和 Bot 的私聊是操作台：用来搜索、入库、看进度和接收通知。
- 书库频道是书架：每本书一条帖子，带封面、标题和“在线阅读”按钮。
- 精选频道只放收藏的 PDF。

几处分开，清理私聊不会误删书库，频道也方便按书浏览和搜索。

**为什么不用官方 Bot API 传文件：**

- 官方 Bot API 上传最多 50MB、下载最多 20MB。实测一本 53 页的作品就有 22MB，长篇必然超限。
- 自建 Bot API Server 能放宽大小限制，但读取时仍要把整个文件下载到本机，而服务器只剩约 5GB 磁盘。
- MTProto 没有以上两个问题。

## 四、核心使用流程

**入库：**

1. 用户发 JM 号，或在搜索结果里点“入库”。
2. 服务器在子进程里下载并解码图片。
3. 打包成 CBZ，记录每页在文件中的偏移。
4. 上传到私人频道。
5. 写入索引，通知用户，删除本地工作文件。

**阅读：** 用户在私聊或频道帖子上点“在线阅读”，在 Telegram 里打开 Mini App。手机请求第 N 页时，服务器先查页面缓存；没命中就只从频道读取这一页的字节，同时预取后面几页。不需要先取回整本，阅读进度保存在服务端。

**收藏：** 用户点“收藏”。服务器从书库频道取回整本到临时目录，转成 PDF，发到精选频道，然后删除临时文件。整个过程不经过手机，也不删除书库频道里的原归档。在 TG 里点开精选频道的 PDF 就能看；收藏的作品也照常可以在 Mini App 里按页阅读。

## 五、关键边界

**云端阅读：** 指手机不用预先下载整本文件，不代表没有网络传输。每一页都要经服务器从 Telegram 中转，翻页速度取决于服务器到 Bot 所在数据中心的延迟，靠预取来弥补。首屏速度要等阶段 0 实测后再下结论。

**归档与阅读：** Telegram 归档和网页阅读是两个独立的能力，由本项目负责把它们连起来。

**精选收藏：** 精选频道和书库频道都在 Telegram 上，存储方式相同，只是改存 PDF、单独归类，并不是独立于 Telegram 的备份。以后如果需要独立备份，再另选外部存储。

**访问权限：**

- 频道是私人的，不等于阅读接口可以公开访问。Bot 和阅读页都要校验 Telegram 用户身份，并检查白名单。目前白名单里只有所有者一人，保留新增用户的功能。
- 本仓库是公开仓库。密钥、会话文件、数据库、频道 ID 和漫画文件一律不提交。
- 只管理有权访问和保存的内容，不绕过任何访问权限。

## 六、开发阶段

| 阶段 | 目标 |
| --- | --- |
| 0. 技术验证 | 用真实的 Bot 和私有频道验证：MTProto 大文件上传、按页读取的延迟、Mini App 身份校验、安卓端的打开效果。 |
| 1. 最小可用书库 | 打通搜索、下载、CBZ 打包、频道归档和索引；完成白名单、任务队列、去重和数据库备份。 |
| 2. 云端阅读 | 完成 Mini App 阅读页、按页读取与预取、页面缓存和进度记录。 |
| 3. 精选收藏 | 完成 PDF 转换、发布到精选频道、收藏书架和失败重试。 |
| 4. 体验完善 | 完善书架、搜索与筛选、索引重建工具和日常维护。 |

首期不开发独立的安卓客户端，也不引入 AI 推荐、复杂的多租户或多平台聊天接入。先打通个人使用的完整流程，再按实际需求扩展。

## 七、参考项目与官方文档

以下链接按用途分类，供选型和开发参考。**列为参考不代表已经集成，也不代表这些项目与本仓库有官方合作关系。**

### 核心依赖

| 项目 | 仓库地址 | 用途 |
| --- | --- | --- |
| **JMComic-Crawler-Python** | [hect0x7/JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) | 内容获取核心（搜索、详情、下载与解码）。直接依赖并跟踪上游更新，只用移动端 API。 |
| **Telethon** | [codeberg.org/Lonami/Telethon](https://codeberg.org/Lonami/Telethon) | MTProto 客户端，负责 Bot 对话、大文件上传和按偏移下载。GitHub 仓库已归档，开发迁到了 Codeberg。 |
| **img2pdf** | [josch/img2pdf](https://gitlab.mister-muffin.de/josch/img2pdf) | 把 JPEG 原样封装进 PDF，不再二次压缩。 |

### 参考与备选

| 项目 | 仓库地址 | 说明 |
| --- | --- | --- |
| **jm-view-server** | [hect0x7/jm-view-server](https://github.com/hect0x7/jm-view-server) | 参考它的阅读交互：下拉、翻页、双页、进度记忆。它面向本地文件夹，并带文件管理功能，所以不直接部署。 |
| **python-telegram-bot** | [python-telegram-bot/python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | 备选：如果更习惯它的对话写法，可以让它只负责交互，文件仍交给 Telethon。 |
| **Telegram Bot API Server** | [tdlib/telegram-bot-api](https://github.com/tdlib/telegram-bot-api) | 备选：未采用，原因见第三节。 |
| **ComicInfo** | [anansi-project/comicinfo](https://github.com/anansi-project/comicinfo) | CBZ 内 `ComicInfo.xml` 元数据的格式规范。 |
| **JM-Cosmos II** | [GEMILUXVII/astrbot_plugin_jm_cosmos](https://github.com/GEMILUXVII/astrbot_plugin_jm_cosmos) | 参考它在聊天机器人里做搜索、打包、发送、权限管理和任务通知的方式。 |
| **JM-PDF-plugin** | [AmethystTim/JM-PDF-plugin](https://github.com/AmethystTim/JM-PDF-plugin) | 参考它的 PDF 生成、缓存和按章节下载。 |
| **jmcomic-ai** | [hect0x7/jmcomic-ai](https://github.com/hect0x7/jmcomic-ai) | 可选的 MCP / Agent Skills 扩展，不在首期范围内。 |

### 官方文档

- [JMComic-Crawler-Python 使用文档](https://jmcomic.readthedocs.io/zh-cn/latest/)：上游库的接口和配置。
- [Telegram MTProto 文件上传与下载](https://core.telegram.org/api/files)：分块上传，以及按偏移下载的规则。
- [Telegram Bot API](https://core.telegram.org/bots/api)：Bot 消息、按钮和文件大小限制。
- [Telegram Mini Apps](https://core.telegram.org/bots/webapps)：Mini App 的入口、交互，以及 `initData` 身份校验。
- [Telethon 文档](https://docs.telethon.dev)：以 Bot 身份登录、发送文件、`iter_download`。
- [Telegram Mini Apps 直达链接](https://core.telegram.org/bots/webapps#direct-link-mini-apps)：用 `startapp` 参数直接打开某一本。

**选型原则：** 优先复用成熟的上游组件。新增代码主要负责四件事：Telegram 存储桥接、书库索引、按页阅读和 PDF 收藏。首期不引入不必要的 AI Agent 或多平台机器人功能。
