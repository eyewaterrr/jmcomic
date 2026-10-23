# 进度

## 当前状态（2026-10-23）

- 项目处于规划阶段，还没有代码。
- 已在目标服务器上完成可行性实测，结果见 [environment.md](environment.md)。
- 已根据实测结果提出架构修订建议，见 [architecture.md](architecture.md)。

## 待确认的决策

| # | 问题 | 建议 |
| --- | --- | --- |
| 1 | 频道文件读写用 MTProto（Telethon），还是 Bot API / 自建 Bot API Server | MTProto |
| 2 | 阅读器自研轻量 Mini App，还是改造 jm-view-server | 自研，交互参考 jm-view-server |
| 3 | Google Drive 收藏用什么格式：CBZ、PDF，还是两者都存 | 默认 CBZ，PDF 作为可选 |
| 4 | Google 账号：是否用专用账号，是否加密上传 | 用专用账号，不加密 |
| 5 | 阅读页用哪个子域名 | `jm.<域名>` |
| 6 | 页面缓存上限 | 1GB |

## 阶段 0 需要准备的东西

- [ ] 在 BotFather 新建一个 Bot，拿到 Token。建议不要和其他 Bot 共用。
- [ ] 在 my.telegram.org 申请 api_id 和 api_hash。
- [ ] 新建一个私有频道，把 Bot 设为管理员，并给它“发布消息”权限。
- [ ] 阶段 3 之前：准备专用 Google 账号，并在 Google Cloud 创建 OAuth 客户端。

## 阶段 0 验证项

- [ ] Telethon 以 Bot 身份登录，确认 Bot 所在 DC
- [ ] 上传 20MB 和 100MB 的 CBZ，记录耗时
- [ ] 测量按页区间读取的延迟：首页和预取
- [ ] Mini App 的 initData 校验，以及安卓端打开阅读页的体验
- [ ] 下载画质参数：比较 WebP 质量 80 和更高质量的效果与体积
- [ ] 主服务常驻内存
