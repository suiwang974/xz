---
feature_ids: [F100]
related_features: [video-sniff, direct-link-engine]
topics: [browser-extension, download-takeover, aria2, netdisk, bittorrent]
doc_kind: spec
created: 2026-10-26
---

# F100: 全量下载接管（浏览器插件 + 通用下载内核）

> Status: proposal（待用户拍板）| Owner: 待定 | 规模：大项目，分阶段落地

## Why（为什么要做）

现在 SniffDL 只会"嗅探网页里的视频并下载"。但用户在浏览器里的真实需求远不止视频：GitHub 上的安装包、网盘分享直链、网页上任意附件、甚至 BT/磁力。IDM 靠"接管浏览器所有下载 + 在视频上悬浮下载按钮"成为装机必备；Motrix 靠"内置 aria2 通用内核"几乎什么都能下。我们要把 SniffDL 从"视频嗅探器"升级成"浏览器下载总管"，同时保留我们独有的杀手锏——**整站深度嗅探**（一个链接抓页面下所有视频），这是 IDM/Motrix 都做不到的。

## What（做成什么样）

一句话：**一个浏览器插件负责"看见并抢下所有下载"，现有桌面 App 升级为"通用下载后端"，两者用本地 RPC 打通。**

能力清单（对齐 IDM + Motrix，并超出）：

1. 接管浏览器所有下载：点任何链接/附件触发下载时，插件拦截、转交桌面 App 下载（IDM 式）。
2. 视频悬浮下载按钮：检测到 `<video>`/m3u8/mp4 时，在播放器右上角浮一个"下载"按钮（IDM 式），点开即下。
3. 整页/整站深度嗅探（我们独有）：一键把当前页/列表页下所有视频批量丢进下载队列。
4. 通用文件下载：GitHub Release、普通 HTTP(S)/FTP 大文件，多连接分段加速、断点续传。
5. BT / 磁力链接下载。
6. 网盘直链：夸克/阿里/百度/115/天翼等分享，解析出直链后带 Cookie/Referer 交给内核下载。
7. 任务中心 + 资源库：队列、速度限制、定时、完成通知、去重、历史。
8. 保留看板娘：下载进度/成功/失败仍由桌面上的角色动画反馈（桃桃最抢眼）。

## 现状盘点（我们已经有的，能复用）

- **yt-dlp**：上千站点视频解析（含 HLS/DASH），已是行业标杆。
- **Playwright 深度嗅探管线**：网络层监听、触发播放、列表页→详情页批量抓取——这正是"整站嗅探"的现成核心。
- **自研多连接直链引擎**：分片并发≤16、单文件连接≤16、断点续传。
- **ffmpeg**：分片合并/转码（m3u8→mp4）。
- **Tkinter GUI + 六主题看板娘动画**：任务反馈壳子现成。
- **Node/EJS、代理跟随系统、Cookie 来源管理**：加速与反爬基建已就位。

结论：视频/流媒体这条线我们比 IDM 还强，缺的是"接管浏览器 + 通用文件 + BT + 网盘"。这些**不必从零造**，用成熟件拼装最快。

## 参考项目拆解（各学什么）

- **IDM**：学它的"接管 + 视频悬浮按钮"交互。机制=插件 observe→cancel→hand off；插件扫 `<video>` 注 DOM 按钮，媒体靠网络层 URL 匹配抓取。
- **Motrix**：学它的"通用内核"。实测：Motrix 2.0（2026-10 复活）= Electron+React+TS，**内置一个打过补丁的 aria2 分支 `1.37.0-motrix.19`，通过带 rpc-secret 的 JSON-RPC 驱动**，还提供 headless server。→ 印证"aria2 当通用内核 + RPC 编排"是正解。
- **下载助手 / LinkSwift（油猴）**：学它"网盘直链→aria2 push"。脚本用登录 Cookie 调站点接口换出限时直链，再 `aria2.addUri` 把 URL+Cookie+UA+Referer 推到 RPC(6800)。代价=**每个盘一套脚本，改版即失效**，是长期维护负担。

## 总体架构（四层）

```
┌─ 浏览器插件 (Chrome MV3 / Edge，Firefox 次之)
│   ├─ downloads.onCreated 监听 → cancel() → 把 {url,headers,filename,referrer} 推给后端
│   ├─ content script 扫 <video>/<source> → 注入悬浮"下载"按钮
│   ├─ background webRequest(非阻塞) 观测媒体 URL/分片(m3u8/ts/mp4)
│   ├─ "深度嗅探本页/整站"按钮 → 让后端用 Playwright 跑批量
│   └─ 极薄 Native Messaging host：仅做安装/配对/上报端口，转发到已运行的 App
│
├─ 本地任务通道：127.0.0.1 JSON-RPC（带 token，仅回环，不触发防火墙）
│   兼容 aria2.addUri 语义 → 第三方插件(油猴/下载助手)也能直接推给我们
│
├─ 下载内核集群（编排，不重复造轮子）
│   ├─ 视频/流媒体：yt-dlp + Playwright 深度嗅探 + ffmpeg（现有）
│   ├─ 通用 HTTP/FTP 大文件：自研多连接引擎（现有）
│   └─ BT/磁力/metalink/复杂直链：内置 aria2c 子进程，App 用 JSON-RPC 驱动
│
└─ 桌面 App（现有 Tkinter 升级）：任务中心 / 资源库 / 限速定时 / 看板娘反馈
```

## 关键技术决策（已按调研结论定调）

1. **接管机制**：MV3 没有"改下载 URL"的 API。正解=插件 `downloads.onCreated` → `cancel()` → 把任务发给 App 重下。可 `pause/resume`，`onDeterminingFilename` 仅 Chrome 支持（改名/改目录）。
2. **插件↔App 通道**：**以本地 HTTP JSON-RPC 为主通道**（回环+token，MV3 加 `localhost` host 权限即免 CORS）；Native Messaging 只做安装/配置，**不做任务通道**（下行限 1MB、随浏览器关闭而死）。
3. **视频嗅探深度**：MV3 **读不了响应体**，blob:/MSE 是盲区。默认走"被动 DOM + 网络层 URL/分片匹配"（无侵入、覆盖绝大多数）；对强 DRM/MSE 站点可选 `chrome.debugger`(CDP) 主动抓，但顶部会有"正在调试此浏览器"横幅——**是否接受横幅需你定**。
4. **BT 内核**：aria2 的 BT/DHT 弱于 libtorrent。用 aria2c 起步够用；重 BT 场景可后续加 libtorrent 兜底。
5. **网盘**：先做**通用"直链 + Cookie/Referer 透传"**（覆盖截图里 aria2 push 那类，零站点耦合）；再按你优先级逐个加"解析脚本"（夸克/阿里/百度…），明确这是**长期维护成本**，不是写完就完。
6. **后端形态**：**复用现有 Tkinter App 升级为后端**（省成本、保看板娘），不重做 Electron。

## 分阶段路线图（每阶段都交付可用东西）

- **P0 打地基（后端 RPC）**：App 内置 127.0.0.1 JSON-RPC 服务（addUri/remove/pause/status/list + token），先能收任务、能排队、能回报进度。→ 立即可被油猴/下载助手直接推。
- **P1 浏览器插件 MVP（接管）**：Chrome MV3 插件，`onCreated`→cancel→推 App；任务中心 UI 落地。→ 达成"接管浏览器所有下载"。
- **P2 视频悬浮按钮 + 整页嗅探**：content script 注按钮 + "深度嗅探本页/整站"接现有 Playwright 管线。→ 对齐并超越 IDM 的视频抓取。
- **P3 通用内核扩展**：内置 aria2c 子进程 + 编排，拿下 BT/磁力/metalink/复杂直链；限速、定时、断点续传统一。→ 对齐 Motrix。
- **P4 网盘直链**：通用直链透传 + 首批解析脚本（按你选的盘）。
- **P5 打磨**：Firefox 版、Native Messaging 安装器、资源库去重、看板娘进度反馈、自动更新。

## 风险与合规

- 网盘解析脚本随站点改版失效，需持续维护；部分盘限制批量下载，注意账号风险。
- DRM 内容（EME/加密流）本方案不保证可下，也不做绕过。
- 接管所有下载需用户显式安装并授权；卸载要干净。
- 大项目、耗积分：严格按阶段落地，每阶段验收后再进下一阶段。

## 我的额外建议（比"完全复刻"更聪明的打法）

1. **不要复刻 Motrix 的 Electron 壳**——我们已有 Python 后端 + 看板娘，只需把 aria2c 当"引擎插件"嵌进来，成本远低于重写。
2. **把我们的 RPC 做成 aria2-RPC 兼容层**：这样市面上所有"推送到 Aria2"的油猴/下载助手/网页按钮**无需改造就能直接推给我们**，等于白捡整个生态，比自研插件铺得更快。
3. **差异化护城河 = 整站深度嗅探 + 看板娘**：IDM/Motrix 都没有"一个链接抓全站视频"和桌面动画反馈，这才是别人替代不了的卖点，值得重点打磨。
4. **悬浮按钮做"智能"**：不止"下载此视频"，还弹"本页共 N 个视频/最高清晰度/批量下载"。

## Decisions（2026-10-26 用户拍板）

- 下载内核：**混合引擎**——视频/流媒体继续 yt-dlp+Playwright+ffmpeg；普通大文件走自研多连接引擎；BT/磁力/复杂直链交给内置 aria2c。
- 网盘：**先只做"通用直链 + Cookie/Referer 透传"**，暂不写具体站点解析脚本（夸克/阿里/百度等留待后续按需求再加）。
- MSE/blob 强站：**不启用 chrome.debugger**，只做无侵入的 DOM+网络层嗅探，保持浏览器顶部无横幅。
- 浏览器范围：**先 Chrome + Edge（MV3 一套通吃）**，Firefox/Safari 暂不做。
- 后端形态：复用现有 Tkinter App 升级为下载后端（不重做 Electron），保留看板娘反馈。

## Next: P0 实现范围（当前开工）

新增 `rpc_server.py`：aria2-RPC 兼容的本地 JSON-RPC 服务（127.0.0.1，token 鉴权，仅回环），实现 `aria2.addUri/remove/pause/unpause/getStatus/tellActive/tellWaiting/tellStopped/getDownloadResult` 子集；任务注册表 + 进度回报；按 URL 类型路由到现有引擎（视频→yt-dlp/Playwright，普通文件→多连接引擎，magnet→占位待 P3）。App 启动时按开关拉起服务，任务落到任务中心。验收：self-test 脚本 POST `aria2.addUri` + `getStatus` 跑通。
