# 进度

## 当前状态（2026-10-24）

- 项目处于规划阶段，还没有正式代码。
- 已在目标服务器上完成可行性实测，结果见 [environment.md](environment.md)。
- 已根据实测结果提出架构修订建议，见 [architecture.md](architecture.md)。
- Bot、api_id/api_hash 和私有频道都已就绪，密钥保存在服务器的密钥文件里，不进仓库。
- 阶段 0 的 Telegram 存储验证已经通过：
  - 并行上传约 7MB/s；
  - 按页读取单页约 0.2 秒。

## 已确定

| 项目 | 结论 |
| --- | --- |
| 收藏格式 | PDF |

## 待确认的决策

| # | 问题 | 建议 |
| --- | --- | --- |
| 1 | 收藏存放位置：R2、Telegram 第二个私有频道、MEGA，还是自建 Komga | 重新讨论中 |
| 2 | 频道文件读写用 MTProto（Telethon），还是 Bot API / 自建 Bot API Server | MTProto（实测已验证） |
| 3 | 阅读器自研轻量 Mini App，还是改造 jm-view-server | 自研，交互参考 jm-view-server |
| 4 | 书库存在私有频道里，还是存在和 Bot 的私聊里 | 私有频道（已创建） |
| 5 | 收藏 PDF 的 JPEG 质量：85 还是 92 | 85 |
| 6 | 阅读页用哪个子域名 | `jm.<域名>` |
| 7 | 页面缓存上限 | 1GB |

## 阶段 0 准备

- [x] 在 BotFather 新建 Bot，拿到 Token
- [x] 把 api_id 和 api_hash 填进服务器上的密钥文件
- [x] 新建私有频道，把 Bot 设为管理员，并开启发布、编辑、删除消息权限

## 阶段 0 验证项

- [x] Telethon 以 Bot 身份登录，确认 Bot 在 DC1（迈阿密）
- [x] 上传 20MB 和 100MB 的 CBZ：顺序上传 0.8MB/s，并行上传约 7MB/s
- [x] 按页区间读取：单页中位数 178ms，并行预取 10 页约 1.1 秒
- [ ] Mini App 的 initData 校验，以及安卓端打开阅读页的体验
- [ ] 频道帖子的 `startapp` 链接能否直接打开对应的作品
- [ ] 下载画质参数：比较 WebP 质量 80 和更高质量的效果与体积
- [ ] 主服务常驻内存
