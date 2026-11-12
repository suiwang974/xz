# SniffDL 下载接管 · 浏览器插件（P1）

把浏览器里的所有下载接管给桌面版 SniffDL（二次元看板娘下载器），走本机 aria2 兼容 JSON-RPC。
支持 Chrome / Edge（MV3）。

## 它做什么

1. 监听浏览器每一次下载 → 命中规则就取消浏览器自己的下载 → 把
   `{URL, 文件名, Referer, Cookie, User-Agent}` 作为 `aria2.addUri` 推给本机 SniffDL。
2. 真正下载由桌面 App 完成（多连接 / 断点续传 / 看板娘成功·失败反馈）。
3. 弹窗自动读取当前世界主题（`sniffdl.theme`）换肤：切到哪一界，插件就是哪界的配色与看板娘标识。

## 安装（开发者模式加载）

1. 打开桌面 SniffDL → 设置里勾选「本地下载服务 (RPC)」，记下端口（默认 6800），点「应用」。
2. Chrome/Edge 地址栏进 `chrome://extensions`（Edge 为 `edge://extensions`）。
3. 右上角打开「开发者模式」。
4. 点「加载已解压的扩展程序」，选择本 `extension` 目录。
5. 点工具栏插件图标 → 「测试连接」应显示「已连接」。随便下个文件试试，会被接管。

## 权限说明（为什么这么多）

- `downloads`：监听并接管下载。
- `cookies` + `<all_urls>`：给需要登录态的直链带上 Cookie（网盘/GitHub 私有等）。
- `http://127.0.0.1/*`：与本机 SniffDL RPC 通信。
- `notifications`：接管成功/失败提示。

## 设置项（弹窗内）

- 接管浏览器下载：总开关。
- 本地端口 / Token：需与桌面 App 设置一致。
- 只接管 ≥ N MB：小文件不抢（0=不限）。
- 扩展名白名单：如 `exe,zip,mp4`，留空=全部类型都接管。

## 已知边界

- 磁力/BT：桌面端 P3 才接入，插件推过去会收到明确"未接入"提示。
- blob:/MSE 加密流：MV3 读不到响应体，这类不接管（按既定决策不启用 chrome.debugger）。
- 弹窗头像暂用世界 motif 图标占位，真实看板娘立绘随后续版本导出打包。

## 0.7.0（v3 内核对接）
- 连上 v3 内核时，接管规则以 SniffDL「设置 › 高级 › 浏览器接管」为准（`sniffdl.takeoverRules`：扩展名、最小大小、总是/从不接管的站点、按住 Alt 点击不接管、主动点下载时先确认）；弹窗里的旧「最小大小/扩展名」只对旧桌面端生效。
- 接管走 `sniffdl.intercept`：开「先确认」→ 进 SniffDL「待确认」；关 → 直接开下。
- 右键新增「用 SniffDL 下载全部链接」（收集本页链接 → `sniffdl.grabber.push` 进待确认）和「用 SniffDL 分析本页」。
- 内核没运行：不吞下载（交回浏览器自己下），系统通知 + 图标红色「!」+ 弹窗写明「SniffDL 没有运行」。
- 旧桌面端（没有 takeoverRules）：行为与 0.6.0 完全一致；视频嗅探、aria2 兼容推送不变。
