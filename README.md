# JMComic · Telegram 私人漫画书库

以 Telegram 为统一入口，结合服务器端内容获取、在线阅读和 Google Drive 精选收藏，构建个人使用的云端漫画书库。

> 当前状态：架构规划阶段。本文描述拟开发的目标与模块边界，不代表相关功能已经实现或部署。

## 一、最终目的

在 Telegram 中完成“查找 → 入库 → 在线阅读 → 收藏”的完整流程：漫画主要归档到私人 Telegram 频道，安卓手机无需先下载整本 PDF 即可阅读；满意的作品可一键另存到 Google Drive，形成独立的精选收藏。

服务器负责内容处理和临时缓存，不长期保存全部漫画。项目优先服务个人或少量授权用户，不面向公众开放下载服务。

## 二、整体框架

```text
Telegram 安卓 / 桌面客户端
          │
          ├── Bot：查询、入库、任务通知、收藏操作
          └── Mini App / 网页阅读器：书架、阅读、进度
                         │
                    VPS 应用服务
                         │
          ┌──────────────┼──────────────┐
          │              │              │
     内容获取引擎    Telegram 私有频道   Google Drive
       jmcomic        主要归档存储        精选收藏
                         │
              数据库索引 + 有限本地缓存
```

图中表示模块分工，不表示客户端直接连接存储；文件传输、权限校验和任务调度统一由服务器处理。

## 三、开发架构

采用“独立业务应用 + 上游能力复用”的方式，初期部署在一台 Ubuntu VPS 上，保持模块化，不引入复杂的微服务体系。

| 模块 | 主要职责 |
| --- | --- |
| Telegram 交互层 | 接收查询、入库和收藏操作，展示任务状态，提供在线阅读入口。 |
| 书库与任务服务 | 统一管理作品、章节、归档状态和收藏状态；负责排队、去重、重试及结果通知。 |
| 内容获取层 | 使用原版 `JMComic-Crawler-Python` 完成内容查询和下载，尽量不修改上游核心代码。 |
| Telegram 存储层 | 通过 Bot API 将归档保存到私人频道，并按数据库记录取回；优先评估复用已有的本地 Bot API 服务。 |
| 阅读服务 | 将服务器缓存中的内容按页提供给手机，支持连续阅读与进度保存；评估复用或改造 `jm-view-server`，与 Telegram 存储桥接。 |
| Google Drive 收藏层 | 经用户授权，将指定作品另存到专用目录，记录保存结果，避免重复上传。 |
| 数据与缓存层 | 初期采用 SQLite 保存书目、存储定位、任务和进度；本地缓存设容量上限，并独立备份数据库。 |

归档格式暂以 CBZ / ZIP 图片包为主，PDF 作为可选导出和收藏格式。具体格式与阅读组件在开发验证后确定。

## 四、核心使用流程

**入库：** 用户在 Telegram 发起请求，服务器获取并整理作品，上传至私人频道，确认归档成功后记录索引并通知用户。本地副本随后按缓存策略清理。

**阅读：** 用户点击“在线阅读”，服务器优先使用缓存；未命中时从 Telegram 取回归档并准备页面。手机按需加载当前页及附近页面，阅读进度保存在服务端。

**收藏：** 用户点击“保存到 Google Drive”，服务器使用缓存或从 Telegram 取回文件，再上传到已授权的收藏目录。该操作不经过手机，也不删除 Telegram 中的原归档。

## 五、关键边界

“云端阅读”指手机不必预先下载整本文件，不代表零网络传输或零临时缓存。未缓存作品首次打开时，服务器可能需要先取回完整归档，因此应展示准备状态，而不是承诺立即打开。

Telegram 归档与网页阅读是两个独立能力，需要本项目负责连接；不把原生 PDF 消息或现成阅读器视为自动具备这项能力。Google Drive 用于主动收藏，不默认镜像整个书库，也不承诺第三方存储永久可用。

私人频道不等于阅读接口可以公开访问。Bot 和阅读页面均需验证身份与访问权限；密钥、授权令牌、数据库和漫画文件不提交到仓库。只管理有权访问和保存的内容，不绕过访问权限。

## 六、开发阶段

| 阶段 | 目标 |
| --- | --- |
| 1. 最小可用书库 | 打通查询、下载、Telegram 归档和索引，建立白名单、任务状态和基础去重。 |
| 2. 云端阅读 | 完成归档取回、按页阅读、进度记录和受限缓存，优先验证安卓端体验。 |
| 3. 精选收藏 | 接入 Google Drive 授权、一键另存、收藏状态和失败重试。 |
| 4. 体验完善 | 完善 Mini App 书架、搜索与筛选、备份恢复及日常维护能力。 |

首期不开发独立安卓客户端，不引入 AI 推荐、复杂多租户或多平台聊天接入。先打通个人使用的完整流程，再按实际需求扩展。

## 七、参考项目与官方文档

以下链接按用途分类，供后续选型和开发参考。**列为参考不代表已经集成，也不代表相关项目与本仓库存在官方合作关系。**

### 核心项目与可复用组件

| 项目 | 仓库地址 | 参考用途 |
| --- | --- | --- |
| **JMComic-Crawler-Python** | [hect0x7/JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) | 原始内容获取核心：搜索、详情、下载与解码，计划直接依赖并跟踪上游更新。 |
| **jm-view-server** | [hect0x7/jm-view-server](https://github.com/hect0x7/jm-view-server) | 网页漫画阅读、移动端适配、阅读进度；需要额外开发 Telegram 归档与本地阅读缓存之间的桥接。 |
| **python-telegram-bot** | [python-telegram-bot/python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | Python 侧 Telegram 机器人开发参考，包括消息交互与按钮处理。 |
| **Telegram Bot API Server** | [tdlib/telegram-bot-api](https://github.com/tdlib/telegram-bot-api) | 官方开源的自托管 Bot API 服务；优先评估复用现有服务来传输和管理频道归档。 |
| **Google API Python Client** | [googleapis/google-api-python-client](https://github.com/googleapis/google-api-python-client) | Google Drive API 的 Python 客户端参考，用于授权后上传精选收藏。 |

### 业务设计与后续扩展参考

| 项目 | 仓库地址 | 值得借鉴的内容 |
| --- | --- | --- |
| **JM-Cosmos II** | [GEMILUXVII/astrbot_plugin_jm_cosmos](https://github.com/GEMILUXVII/astrbot_plugin_jm_cosmos) | 聊天机器人内的漫画搜索、打包、发送、权限管理和任务通知。 |
| **JM-PDF-plugin** | [AmethystTim/JM-PDF-plugin](https://github.com/AmethystTim/JM-PDF-plugin) | PDF 生成、缓存、按章节下载与消息交互。 |
| **jmcomic-ai** | [hect0x7/jmcomic-ai](https://github.com/hect0x7/jmcomic-ai) | 可选的 MCP / Agent Skills 扩展；不属于首期开发范围。 |

### 官方接口与使用文档

- [JMComic-Crawler-Python 使用文档](https://jmcomic.readthedocs.io/zh-cn/latest/)：上游库接口和配置。
- [Telegram Bot API](https://core.telegram.org/bots/api)：机器人消息、文件及频道操作接口。
- [Telegram Mini Apps](https://core.telegram.org/bots/webapps)：Telegram 内嵌网页应用的入口、交互及身份验证。
- [Google Drive API](https://developers.google.com/workspace/drive/api/guides/about-sdk)：云端文件操作、授权与上传相关文档。

**选型原则：** 优先复用成熟上游组件，新增代码主要承担 Telegram 云存储桥接、书库索引、在线阅读接入及 Google Drive 收藏。首期不引入不必要的 AI Agent 或多平台机器人功能。
