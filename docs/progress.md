# 进度

## 当前状态（2026-10-23）

- 项目处于规划阶段，还没有代码。
- 已在目标服务器上完成可行性实测，结果见 [environment.md](environment.md)。
- 已根据实测结果提出架构修订建议，见 [architecture.md](architecture.md)。
- Bot 已创建，Token 已验证可用，保存在服务器的密钥文件里，不进仓库。

## 已确定

| 项目 | 结论 |
| --- | --- |
| 收藏格式 | PDF |
| 收藏位置 | Cloudflare R2（私有桶），不用 Google Drive |

## 待确认的决策

| # | 问题 | 建议 |
| --- | --- | --- |
| 1 | 频道文件读写用 MTProto（Telethon），还是 Bot API / 自建 Bot API Server | MTProto |
| 2 | 阅读器自研轻量 Mini App，还是改造 jm-view-server | 自研，交互参考 jm-view-server |
| 3 | 书库存在私有频道里，还是存在和 Bot 的私聊里 | 私有频道 |
| 4 | 收藏 PDF 的 JPEG 质量：85 还是 92 | 85 |
| 5 | 阅读页用哪个子域名 | `jm.<域名>` |
| 6 | 页面缓存上限 | 1GB |

## 阶段 0 需要准备的东西

- [x] 在 BotFather 新建 Bot，拿到 Token
- [ ] 把 my.telegram.org 的 api_id 和 api_hash 填进服务器上的密钥文件
- [ ] 新建私有频道，把 Bot 设为管理员，并给它“发布消息”“编辑消息”“删除消息”权限
- [ ] 阶段 3 之前：新建 R2 桶，并创建只能读写这个桶的 API 密钥

## 阶段 0 验证项

- [ ] Telethon 以 Bot 身份登录，确认 Bot 所在 DC
- [ ] 上传 20MB 和 100MB 的 CBZ，记录耗时
- [ ] 测量按页区间读取的延迟：首页和预取
- [ ] Mini App 的 initData 校验，以及安卓端打开阅读页的体验
- [ ] 频道帖子的 `startapp` 链接能否直接打开对应的作品
- [ ] 下载画质参数：比较 WebP 质量 80 和更高质量的效果与体积
- [ ] 主服务常驻内存
