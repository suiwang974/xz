# qBittorrent 增强版（EE）升级 + 提速 + 订阅 + 功能复刻方案

> 目标：把 c0re100/qBittorrent-Enhanced-Edition 从 **5.2.3.10（LT2.1 实验版）** 升级到 **5.2.4.10**，并完成提速、Tracker 订阅、RSS 订阅的落地配置，同时盘点 EE 独有功能、规划可迁移到自研下载器 SniffDL 的工程思路。
>
> 本文档为**纯调研 + 方案**，不含任何自动安装/改机器操作。所有对机器现状的描述均来自对你目录的只读检查；所有对参数的取值分两类：**【事实】**（有官方/源码来源）与 **【建议】**（我的经验判断）。

---

## 结论先行（TL;DR，30 秒版）

1. **升级很安全，不会丢配置和任务**：qBittorrent 的配置和断点续传数据存在 `AppData`，**和安装目录 `D:\wws\BT\qBittorrent` 完全分离**。直接覆盖安装即可保留；实测你当前 `BT_backup` 里 **0 个已保存任务**，升级风险极低。
2. **最大的隐性变化是 libtorrent 版本**：你旧的 exe 是 **实验性 LT2.1** 构建（文件名带 `LT2.1`）；EE 官方 5.2.4.10 **只发布单一 Windows 构建、不再带 LT 标记**。装完务必用 **帮助 → 关于** 核对 libtorrent 版本，这直接决定"磁盘缓存"该怎么设（LT1.2 与 LT2.x 机制不同）。
3. **提速三件最值的事**（个人重度下载、公网 BT）：① **打开队列系统、限制同时下载任务数**（你现在 `QueueingSystemEnabled=false`，带宽被摊薄）；② **固定监听端口 + 开 UPnP/端口转发**（你现在是随机高位端口 65193，没人连得进来）；③ **DHT + PeX + µTP 保持开、连接数别虚高**，并 **开启"向所有 tracker 同时 announce"** 配合订阅源。
4. **Tracker 订阅你其实已经在用了**：ini 显示 `AdditionalTrackersURL = ngosang trackers_best.txt` 且开关为 `true`。建议再叠加 XIU2 每日更新源做冗余。
5. **RSS 自动下载你也已配好规则**（SmartEpisodeFilter + DownloadRepacks）。本文补机制说明与优化点。
6. **给 SniffDL 的可迁移思路**在最后一节，按"连接调度 / 多源并发 / 失败重试 / 假源过滤"来讲——只借思路，不移植 BT 代码。

---

## 0. 现状快照（只读实测你的机器）

**安装目录 `D:\wws\BT\qBittorrent`：**
| 文件 | 说明 |
|---|---|
| `qbittorrent.exe` | 当前已安装运行版本（旧 5.2.3.10，LT2.1） |
| `qbittorrent_enhanced_5.2.3.10_x64_LT2.1_setup.exe` | 旧安装包（文件名带 **LT2.1**，即实验性 libtorrent 2.1 构建） |
| `qbittorrent_enhanced_5.2.4.10_x64_setup.exe` | **你已下载好的新版**，28.9MB，与 GitHub 官方 5.2.4.10 的 x64 安装包文件名完全一致 |
| `uninst.exe` / `qt.conf` / `translations/` | 卸载器与运行时资源 |

**配置数据（与安装目录分离，升级不动它们）：**
- 主配置：`%APPDATA%\qBittorrent\qBittorrent.ini`（Roaming）——当前 6.6KB，绝大多数调优项**没写出来 = 用默认值**。
- 断点续传/会话：`%LOCALAPPDATA%\qBittorrent\BT_backup\`——实测 `*.fastresume` **数量 = 0**（当前无保存的下载任务）。
- EE 缓存的订阅 tracker：`%LOCALAPPDATA%\qBittorrent\additional_trackers_from_url.txt`（当前约 20 条，来自 ngosang best）。
- EE 用于"屏蔽中国未知 peer"的 IP 库：`%LOCALAPPDATA%\qBittorrent\GeoDB\`（存在，说明 EE 反吸血相关功能在用）。
- RSS 数据：`%APPDATA%\qBittorrent\rss\`（在用）。

**你当前几个关键设置（事实，读自 ini）：**
| 项 | 现值 | 影响 |
|---|---|---|
| `Session\QueueingSystemEnabled` | **false（未开队列）** | 所有任务同时抢带宽 → 提速要改 |
| `Session\Port` / `SSL\Port` | **65193 / 39826** | 随机高位端口，无端口映射 → 影响被连接 |
| `Session\AddTrackersFromURLEnabled` | **true** | EE 自动订阅 tracker 已开 |
| `Session\AdditionalTrackersURL` | `https://ngosang.github.io/trackerslist/trackers_best.txt` | 你已在用 ngosang best |
| `AutoDownloader\SmartEpisodeFilter` / `DownloadRepacks` | 已配置 / true | RSS 自动下载已配置 |
| 连接数/磁盘缓存/异步IO/µTP/DHT/加密 | **全部缺省（默认值）** | 下面第 2 节逐项给默认值与推荐值 |

---

## 1. 升级步骤：5.2.3.10 → 5.2.4.10（正确姿势）

### 1.1 关键事实（先看清楚）

- **【事实】版本沿革**：EE `5.2.3.10`（2024-07-19）changelog = "Official Update (v5.2.3)" + **"[实验性/仅Windows] libtorrent 2.1 构建 (#715, 由 @leo9800 backport)"**；因此该版有 **两个 Windows 安装包**：`..._x64_setup.exe`（稳定默认）与 `..._x64_LT2.1_setup.exe`（实验）。你装的是后者。
  来源：[EE Releases](https://github.com/c0re100/qBittorrent-Enhanced-Edition/releases) 、release tag `release-5.2.3.10` 的资产列表与 body。
- **【事实】新版 5.2.4.10**（2024-10-03）changelog 只有一行："**Official Update (v5.2.4)**"；Windows **只发一个** `qbittorrent_enhanced_5.2.4.10_x64_setup.exe`（无 LT 标记）。也就是 **LT2.1 实验分支在 5.2.4.10 被收回，改回单一稳定构建**。
- **【事实】libtorrent 具体内嵌版本无法从版本号直接断定**：EE 的 Windows CI 同时构建 `RC_2_0` 与 `RC_1_2` 两条 libtorrent 线（见仓库 `.github/workflows/ci_windows.yaml` 的 matrix），**发行包默认对应稳定线**。→ **不要猜**，装完后 **帮助 → 关于** 里会显示 `libtorrent x.y.z`，以此为准（这决定第 2 节磁盘缓存策略）。

### 1.2 配置/任务是否保留？需不需要先卸载？

- **【事实】配置与任务与安装目录分离**（见 §0），Windows 覆盖安装**不会**动 `AppData`，因此**设置项、RSS 规则、订阅源、历史下载任务都会保留**。
- **【事实】EE 官方 setup 支持就地覆盖安装**（同一 NSIS 安装器、同一 Program 目录）。
- **【建议】更稳妥的路线（推荐）**：先备份 → 卸载旧版（`uninst.exe` **只删程序本体**，不动 AppData）→ 装新版到同目录。这样能清掉旧版可能残留的 DLL（尤其 LT2.1 相关），避免"新旧 libturtle 混装"。
- **【建议】懒人路线**：直接双击 `qbittorrent_enhanced_5.2.4.10_x64_setup.exe` 覆盖安装到 `D:\wws\BT\qBittorrent`。绝大多数情况没问题；若装后异常再走"卸载重装"。

### 1.3 标准升级流程（照做即可）

> 全程**先退出正在运行的 qBittorrent**（托盘图标右键 → 退出；确认任务管理器无 `qbittorrent.exe`）。

1. **备份配置（5 分钟保险）**：复制以下两处到别处
   - `%APPDATA%\qBittorrent\`（`qBittorrent.ini`、`qBittorrent-data.ini`、`categories.json`、`rss\`）
   - `%LOCALAPPDATA%\qBittorrent\`（`BT_backup\`、`GeoDB\`、`nova3\`、`additional_trackers_from_url.txt`）
   - 可选：GUI 里 **工具 → 选项 → 导出/导入**（部分版本支持），或整目录打 zip。
2. **记录可回滚信息**：记下当前版本号、监听端口 65193、订阅 URL（已在 ini，备份即含）。
3. **卸载旧版（推荐路线）**：运行 `D:\wws\BT\qBittorrent\uninst.exe`；**保留 AppData**（卸载器默认不动）。→ 想留个性化可跳过，直接下一步覆盖装。
4. **安装新版**：双击 `qbittorrent_enhanced_5.2.4.10_x64_setup.exe`；**安装路径填 `D:\wws\BT\qBittorrent`**；按提示完成（勾"开机/桌面快捷方式"按需）。
5. **首次启动核对**：**帮助 → 关于** 记录 `qBittorrent 5.2.4.10` + `libtorrent x.y.z` + `Qt x.y`（写进你的笔记，后续调磁盘缓存要用）。
6. **逐项验证**：
   - 监听端口是否还是 65193（若被重置，回到第 2 节重设 + 端口映射）。
   - Tracker 订阅：`additional_trackers_from_url.txt` 是否还在自动更新（工具→选项→BitTorrent 里"自动添加以下tracker"URL 是否还是 ngosang best）。
   - RSS：阅读器与自动下载规则是否还在。
   - 若之前有下载任务：列表应为空（你 BT_backup=0），手动添加种子测试跑一条即可。
7. **清理旧安装包**（可选）：确认新版稳定后，旧 `..._5.2.3.10_..._setup.exe` 可删；新版安装包留着方便重装。

### 1.4 风险点清单（务必逐条心里有数）

| 风险 | 说明 | 规避 |
|---|---|---|
| **在 PT 站用增强版被 ban** | 【事实】EE 官方 NOTE.md 明确警告：不要用于 Private Tracker，除非该 PT 允许；EE 的 **UA=`qBittorrent Enhanced/版本`、PeerID=`-qBxxx[A-Z]-`** 与官方不同，易被识别。 | 公网下载用 EE；**PT 另装官方版 qBittorrent**（EE 支持 portable 多实例，见 NOTE.md）。 |
| **libtorrent 版本变化影响内存/磁盘** | 旧版 LT2.1（mmap 内存映射）内存/磁盘 I/O 行为与稳定线不同；新版可能回退到 1.2/2.0 线，**磁盘缓存含义随之改变**。 | 以"关于"里的 libtorrent 版本为准，按 §2 磁盘缓存两种策略选其一。 |
| **随机高位端口 → 升级后仍未做映射** | 65193 没人能主动连你，做种/下载都吃亏。 | §2 固定端口 + UPnP/端口转发。 |
| **配置迁移版本** | ini 里 `Meta\MigrationVersion=8`；跨大版本首次启动会迁移设置，个别第三方插件/WebUI/旧脚本可能不兼容。 | 升级前备份可回滚；插件（如 search/nova3）装后验证。 |
| **GeoDB / 订阅缓存过期** | 升级后 IP 库与 `additional_trackers_from_url.txt` 需联网刷新；离线时"屏蔽中国未知peer"暂时失效。 | 装后联网让它自动更新一次。 |
| **开机自启 / 防火墙规则** | 覆盖安装可能重置防火墙放行规则，导致新端口不通。 | 装后在 Windows Defender 防火墙确认 `qbittorrent.exe` 入站放行 + 新端口。 |
| **回滚** | 若新版异常：卸载新版 → 重装 `..._5.2.3.10_..._LT2.1_setup.exe` → 还原备份的 AppData。 | 备份是关键，别跳。 |

---

## 2. 提速配置清单（qBittorrent 5.2.x）

> 说明：BT 里"最大连接数"指 **peer 连接数**（不是 HTTP 的"每服务器连接数"）；你提到的 **"每服务器连接数"是 aria2/IDM 的 HTTP 下载概念**，BT 无此对应项，最接近的是"每个 torrent 的最大 peer 数 / 向所有 tracker announce"。相关差异已在下表注明。
>
> 下面**默认值**均引自 qBittorrent 源码 `src/base/bittorrent/sessionimpl.cpp`（master≈5.2.x）与官方 wiki，避免口胡。GUI 路径以中文界面为准。

### 2A. 无脑照抄的安全值（对所有人基本只有好处）

| 设置项（GUI 路径） | 源码默认【事实】 | 推荐值 | 为什么 |
|---|---|---|---|
| **加密模式**（BitTorrent → 加密模式） | `Encryption=0`（允许加密，非强制） | 保持 **允许/首选**（=0），别选"强制" | 强制加密会挡掉大量只支持明文的 peer，反而变慢；0 已能协商。【事实：源码 encryption=0】 |
| **µTP（替代协议）**（连接 → 启用 µTP） | 默认启用 | **开** | µTP（TCP-friendly）在路由器/QoS 下更稳、抗限速；关掉会损失一部分只做 µTP 的 peer。 |
| **µTP-TCP 混合模式算法** | wiki 默认 **Prefer TCP** | 保持 **Prefer TCP** | µTP 设计上是给 TCP 让路的；"Prefer TCP"对高带宽更快。【事实：wiki Explanation of Options】 |
| **向所有 tracker 同时 announce**（BitTorrent → Announce to all trackers） | `AnnounceToAllTrackers=false` | **改为 true** | 同时向订阅来的多个 tracker 上报，命中更多 peer → 提速。你当前是默认 false，**这是白捡的一档**。【事实：源码默认 false】 |
| Announce 到所有 tier | `AnnounceToAllTiers=true` | 保持 **true** | 分层 tracker 全覆盖。 |
| OS 缓存（Advanced → Enable OS caching） | wiki 默认 **启用** | 保持 **启用** | 让系统页缓存帮你缓冲，通常更快。【事实：wiki】 |
| Coalesce reads & writes（合并读写） | wiki 默认 **启用** | 保持 **启用** | 合并小 IO，降磁盘负担。 |
| 磁盘缓存到期（Advanced → Disk cache expiry） | `DiskCacheTTL=60`（秒） | 保持 **60**，除非内存紧 | 频繁强制刷盘会拖慢；默认够用。【事实：源码 60】 |
| **µTP / DHT / PeX**（见下 2B 也有） | DHT/PeX/LSD 默认 **true** | 见 2B（LSD 公网建议关） | — |

### 2B. 看你的网络情况调（带宽/线路决定，收益最大但需验证）

| 设置项（GUI 路径） | 源码默认【事实】 | 推荐值（个人重度、高速光纤） | 为什么 / 何时该改 |
|---|---|---|---|
| **最大连接数（全局）**（连接 → 最大连接总数） | `MaxConnections=500` | **500（保持）**；线路差/路由老可降到 300–400 | 连接数虚高不会更快，反而增加握手开销与 CPU/内存，家用路由 NAT 表也扛不住。500 是官方默认，一般不用动。【事实：源码 500】 |
| **每 torrent 最大连接数**（连接 → 每 torrent 上限） | `MaxConnectionsPerTorrent=100` | **100（保持）** | 单个种子 100 peer 已足够填满带宽；再高边际收益极低。【事实：源码 100】 |
| **全局上传槽（每 torrent 上限）** | `MaxUploads=20` / `MaxUploadsPerTorrent=4` | 保持 **20 / 4**（纯下不种可不管） | 上传槽影响做种效率与"以传带下"；BT 里给对方上传才能换来对方给你下，别把上传压到 0。【事实：源码 20 / 4】 |
| **半开连接数**（若界面还有） | `ConnectionSpeed=30` | **保持默认**；新版 GUI 多已移除 | 【事实】官方 wiki：该选项因"从 libtorrent 移除、不再有意义"已废弃。**别为找这个项折腾**。 |
| **下载/上传速率上限**（速度 → 上行/下行限制） | 默认 **0 = 不限** | **0（不限）** | 除非你上行被运营商限速、或要保网页体验，否则设 0。想限制时**留约 20% 上行余量**给 ACK/tracker，下行才不被 ACK 阻塞拖慢。 |
| **磁盘缓存 Disk cache**（高级 → 磁盘缓存，单位 MiB） | `DiskCacheSize=-1`（源码把 -1 透传 libtorrent = **自动**） | **LT1.2 线：内存充足设 `256`–`1024`；LT2.x 线：保持 `-1/自动` 并重点看内存占用** | 【事实】源码里 `-1` = 交给 libtorrent 自适应；`0` = 关闭缓冲直接落盘。**LT 1.2.x**：加大缓存能减少磁盘往返、提速。**LT 2.0/2.1 用内存映射(mmap)**，磁盘缓存设置对内存影响弱化、反而可能内存/IO 飙升（社区大量反馈，见 qBittorrent issue #22050）。**故务必先在"关于"确认 libtorrent 版本再定**。【建议】内存 <8GB 或机械盘：别设太大，配合"到期 30–40s"。参考社区实测：`2048MiB 缓存 + 到期 <40s` 适合高带宽。 |
| **异步 I/O 线程数**（高级 → Async I/O threads） | `AsyncIOThreadsCount=10` | **CPU 核数 ×4**（如 8 核=32），**上限 ~32** | 【事实】wiki：默认 10；建议设为硬件线程数的 4 倍；再高无收益。校验/哈希也吃这个。 |
| **Socket 发送缓冲区**（高级 → Socket send/receive buffer） | `SocketSendBufferSize=0`（自动） | **0（自动）**；仅高速做种/万兆可手动 1–4 MiB | 【事实】默认 0=系统自动。另：`SendBufferWatermark=500KiB`，wiki 建议**高速上传把"watermark factor"调到 >100%**（默认 50）。 |
| **连接类型 / 监听端口**（连接 → 监听端口 / UPnP / IPv6） | 你现 `Port=65193`（随机高位） | **改成一个固定端口（如 16384–60000 任选一记牢的），开 UPnP/NAT-PMP，路由器做端口转发** | 【事实】可连进来的 peer 越多越快；随机端口 + 无映射 = 大多 NAT 后 peer 连不到你，**是提速最被低估的一环**。固定端口后才能稳定转发。【建议】同时开 IPv6（有则）扩大可达 peer 池。 |
| **DHT / PeX**（BitTorrent → 启用 DHT / PeX） | 默认 **true** | **保持开启** | 无 tracker/tracker 挂了时靠 DHT+PeX 找 peer，提速兼抗 tracker 失效。【事实：源码 true】 |
| **LSD 本地发现**（启用 LSD） | 源码默认 **true** | **公网下载建议关** | 【事实：源码 true】LSD 只在**同一局域网**互相发现有用；公网开着徒增广播，关掉更干净。家庭内网共享才需要开。 |
| **队列系统 / 最大活跃任务**（BitTorrent → 队列 + 最大活跃下载/做种） | `QueueingSystemEnabled=false` | **改为 true**；最大活跃下载 **3–5**、最大活跃做种按上行定（如 10–20） | 【事实：你当前 false】不排队 = 全部同时抢带宽 → 每个都慢。开启并**限制同时下载数**，让带宽集中到少数种子，单任务更快、更快出种子。**这是个人用户提速最直接的一招。** 另勾"忽略慢速种子"（`IgnoreSlowTorrents`，默认 false，建议开）避免死种子占坑。 |
| **共享率限制**（队列 → 达到分享率后） | `GlobalMaxRatio=-1`（不限制） | 个人按需设如 **2.0–5.0** 或保持不限 | 只做种不占下行带宽即可设比率上限，让老种子自动停、腾出活跃位。 |
| **多同 IP 连接**（连接 → 允许同一 IP 多连） | `MultiConnectionsPerIp=false` | **保持 false** | 默认关闭可防滥用/刷连接；开了对提速帮助不大。【事实：源码 false】 |

### 2C. EE（增强版）特有：反吸血 / 白名单相关（提速 + 保数据质量）

> 这些是 EE 独有开关（见 §5 源码盘点）。位置大致在 **工具 → 选项 → BitTorrent（增强选项）** 与各菜单。

| EE 设置 | 推荐 | 解决什么 |
|---|---|---|
| **Auto Ban 吸血客户端**（迅雷/QQ/百度网盘/xfplay/DLBA/离线下载器） | **保持开（EE 默认核心功能）** | 这些"只下不上/伪造"的吸血 peer 会占你连接和上行却不回传，屏蔽后带宽留给真 peer → **提速 + 提高数据真实性**。【事实：README】 |
| **Auto Ban Unknown Peer from China**（默认 OFF） | 看场景：公网下载可 **开** | 屏蔽"未知 UA 且来自中国大陆"的可疑假 peer（多为吸血/伪造）。用 `GeoDB\` IP 库判定（你机器已存在）。注意会误伤少量合法节点，介意则关。【事实：README + 源码】 |
| **Auto Ban BitTorrent Media Player Peer**（默认 OFF） | 可选 **开** | 屏蔽 StellarPlayer/UW/SP 等"边下边播播放器"伪节点。【事实：README + 源码】 |
| **Auto Update Public Trackers List**（默认 OFF） | **开**（你已开订阅 URL） | EE 定时拉取你设的订阅 URL，自动为新种子补充 tracker → 提升可发现 peer 数。见 §3。【事实：README + 源码 updateTrackersFromURL】 |
| **Peer 白名单/黑名单 + IP 子网白名单** | 进阶按需 | 对可信来源（如某 PT/某内网）走白名单优先连接。EE 提供 `ipsubnetwhitelist` 与 banlist 两套对话框。【事实：源码文件 `ipsubnetwhitelistoptionsdialog` / `banlistoptionsdialog` / `peer_blacklist.hpp`】 |

> **提速顺序建议**：先做 2A 全表（零风险）+ 2B 里"开队列限活跃数""固定端口+UPnP""向所有 tracker announce"这三项，收益最大；磁盘缓存/异步线程这类再按 libtorrent 版本与硬件微调，改一项测一项（看"下载速度/被连接状态"）。

---

## 3. Tracker 订阅方案

### 3.1 高质量公开 tracker 来源（推荐 2–3 个做冗余）

| 来源 | 说明 | 可粘贴订阅 URL |
|---|---|---|
| **ngosang/trackerslist**（你已在用其一） | 社区公认最主流的精选 tracker 列表，定期人工筛选 | `https://ngosang.github.io/trackerslist/trackers_best.txt`（精选，你现用）；`.../trackers_all.txt`（全量）；raw 版 `https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt`。来源：[ngosang/trackerslist](https://github.com/ngosang/trackerslist) |
| **XIU2/TrackersListCollection**（国人维护，**每日自动更新**，含大量国内可达源） | 附带 CDN 镜像、含 best/all 分档，适合叠加冗余 | `https://cf.trackerslist.com/all.txt`（全）；`https://cf.trackerslist.com/best.txt`（精选）；jsDelivr `https://cdn.jsdelivr.net/gh/XIU2/TrackersListCollection/best.txt`；Fastly `https://fastly.jsdelivr.net/gh/XIU2/TrackersListCollection/best.txt`。来源：[XIU2/TrackersListCollection](https://github.com/XIU2/TrackersListCollection) |
| （可选）**合并去重自定义源** | 把上面两源合并去重，托管成自己的一个 txt | 【建议】见下 3.3 自动更新做法 |

### 3.2 在 qBittorrent 里怎么贴（两种方式）

- **方式 A：内置订阅（EE 自带，推荐，你已启用）**
  工具 → 选项 → **BitTorrent** → 勾选 **"自动添加以下 tracker 到新下载" / "Automatically add these trackers to new downloads"**，把 URL 填进去。
  - 你当前 ini：`AddTrackersFromURLEnabled=true`，`AdditionalTrackersURL = ngosang best` → **已经在自动订阅**，EE 会定时刷新缓存到 `%LOCALAPPDATA%\qBittorrent\additional_trackers_from_url.txt`（源码有 `updateTrackersFromURL` 定时器）。
  - 【建议】把 URL 换成/追加 **XIU2** 源做冗余（国内可达性更好），或二者内容手动合并（见 3.3）。
- **方式 B：手动批量添加（一次性给现有种子）**
  上面输入框也支持**直接粘贴多行 tracker 文本**（不是 URL 时当作 tracker 列表）。把 ngosang/XIU2 的 `.txt` 内容全选粘贴即可。

> 注意：**这些公开 tracker 只对"公开种子"有效**。对 PT 私有种子**千万不要**塞公开 tracker——会 announce 到错误服务器、可能触发 PT 封号（见 §1.4 风险 + EE NOTE.md）。

### 3.3 自动更新 tracker 的做法（进阶）

1. **用 EE 内置**（最省事）：保持开关 true + URL，EE 自己定时拉。【事实：功能存在且你在用】
2. **External Program / 计划任务**：写个脚本（curl 下载 XIU2 `best.txt` + ngosang `trackers_best.txt` → 合并 → `sort -u` 去重 → 存成 `my_trackers.txt`），用 **Windows 计划任务**每日跑；再把 `my_trackers.txt` 托管到本地路径/GitHub raw 填进订阅框。这样你有一个"永远最新且去重"的自选源。
3. **社区脚本**：贴吧/博客有"自动更新 qBittorrent tracker 列表"小程序，思路同上（定时抓取 + WebAPI/RPC 批量写），不必依赖 EE 内置。【建议，注意脚本来源可信度】

---

## 4. RSS 订阅 + 自动下载方案

### 4.1 机制（qBittorrent 内置 RSS，你已经配了）

- **RSS 阅读器**：工具 → **RSS（RSS 阅读器）**。左侧 **新增订阅源**（填一个 feed URL，qB 按周期抓取条目，右侧显示条目）。你 ini 里 RSS 面板 `Visible=true`，说明在用。
- **自动下载规则（AutoDownloader / download patterns）**：**RSS → 自动下载（Auto Download Rules）→ 新建规则**。一条规则核心字段：
  - **Name**：规则名。
  - **Every**：抓取周期（分钟）。
  - **Assigned category**：命中后自动归到某分类（决定保存路径，你的下载目录历史是 `D:\wws\BT\SP`）。
  - **Add paused**：命中后是否暂停（想人工确认就勾）。
  - **Episode name（正则/通配）**：决定"抓哪些条目"，见 4.2。
  - **Smart Episode Filter（智能季集过滤）**：你 ini 已配 `s(\d+)e(\d+)`、`(\d+)x(\d+)`、日期式等正则 → **只下"最新一集"、跳过旧的**。`DownloadRepacks=true` → 允许 Repack/重制版本。【事实：读自你的 ini】
  - **Do not use RSS feed until…（去重）**：已下过的不再重复触发。

### 4.2 示例配置思路（不涉及具体资源站，讲透机制）

> 场景：追某番剧，每集出就自动下、存进分类 `SP`、只要最新一集。

1. **RSS → 新增订阅源**：填该站/该分区的 RSS feed URL（公开站通常页面就有 RSS 图标）。
2. **RSS → 自动下载 → 新建规则**：
   - `Name` = `番剧-某作品`
   - `Every` = `60`（每小时轮询）
   - `Assigned category` = `SP`（或新建分类绑定 `D:\wws\BT\SP`）
   - `Episode name` 用正则描述你想抓的：
     - 想抓"含关键词"的条目：在"匹配名称"里写作品关键字；
     - 想排除广告/生肉：`Must not contain` 填 `PV|广告|[72]0p生肉` 之类。
   - `Smart Episode Filter` = `s(\d+)e(\d+)` / `(\d+)x(\d+)`（**你已配好**，保证只下新一集）。
   - 勾选 **Download repacks**（你已 `true`）、按喜好勾 **Add paused** 决定是否人工复核。
3. 保存后，规则命中 feed 新条目 → 自动生成下载任务、归入分类、套用去重。
4. **配合提速**：分类可绑定"限速上限/是否首尾优先",队列里这些任务仍受 §2B 的"最大活跃下载数"约束——追多条源时记得设上限，别把带宽一次性摊薄。

> 【建议】把"公开 RSS 自动化下载"用于**聚合类/公开源**；有登录/隐私的源不要用 RSS 明文托管，避免 feed token 泄露。

---

## 5. 增强版（EE）功能盘点 + 可借鉴进 SniffDL 的思路

### 5.1 EE 相比官方版多出来的功能（完整盘点，事实来源：README + 源码）

| EE 功能 | 解决什么问题 | 实现/判定依据（源码事实） |
|---|---|---|
| **① 自动屏蔽吸血客户端（迅雷/QQ/百度/xfplay/DLBA/离线下载器）** | 这些客户端常"只下不传/伪造 peer"，抢占你的连接与上行却不回传数据，拖累真 peer 交换 | `peer_blacklist.hpp`：PeerID 正则 `-(XL\|SD\|XF\|QD\|BN\|DL\|TS)(\d+)-`（XL=迅雷、QD=QQDownload、XF=xfplay、SD/TS=脱兔/神马系…）；UA 正则匹配 `cacao_torrent` 等；命中即 `drop_connection`。 |
| **② 自动屏蔽"未知 + 中国大陆"peer（默认 OFF）** | 大量吸血/假节点特征是"无 UA(Unknown) 且 CN"，屏蔽减少无效连接 | `is_*_from_China`：`client=="Unknown" && country=="CN"`，靠 **GeoDB** IP 库判定（你机器 `GeoDB\` 已存在）。 |
| **③ 自动屏蔽"网盘离线下载器"** | 云盘离线伪装 BT 客户端薅上传 | 判定 `port>=65000 && country=="CN" && client contains "Transmission"`。 |
| **④ 自动屏蔽"BT 媒体播放器"peer（默认 OFF）** | 边下边播播放器制造大量无效/只下 peer | 判定 `client=="StellarPlayer"` 或 PeerID `-UW\w{4}-` / `-SP\d{4}-`。 |
| **⑤ 自动更新公开 Tracker 列表（默认 OFF）** | 省去手动维护 tracker，为新种子自动补 tracker 提 peer 命中率 | `updateTrackersFromURL` 定时器 + `additional_trackers_from_url.txt` 缓存（你在用，URL=ngosang）。 |
| **⑥ Peer 白名单 / 黑名单** | 精细控制允许/禁止的连接来源 | `peer_filter.hpp`（正则文件加载 peer_id+client）、`ipsubnetwhitelistoptionsdialog`（IP 子网白名单）、`banlistoptionsdialog`（黑名单）、`peer_filter_plugin.hpp`/`peer_filter_session_plugin.hpp`（以 libtorrent torrent/session 插件形式在连接建立时过滤）。 |

> **你提到的 "only_white_list / 白名单反吸血"**：EE 官方 README 里对应的是 **"Peer whitelist/blacklist"** 功能（含 IP 子网白名单对话框），机制是"用正则/网段对 peer 做允许/拒绝"，本质是 **对可疑来源的准入控制**，配合上面 ①–④ 的自动黑名单一起构成"假源过滤"。README 并未使用字面 "only_white_list" 这一设置名（该叫法多见于教程口述），落地时请在 **工具→选项→BitTorrent/增强菜单** 里对照 "白名单/黑名单 / IP 子网白名单" 找。【事实：README 5 条功能 + 源码文件名】

**EE 的核心工程范式**：**把"识别可疑连接"做成 libtorrent 的 plugin，在握手/连接阶段直接丢弃**（`create_*_plugin → peer_filter → drop_connection`）。一句话：**"边连边筛，坏源即时断开"**。

### 5.2 这些思路哪些能移植到 SniffDL（HTTP/流媒体嗅探 + yt-dlp/aria2 下载器）？怎么移植？

> 前提：SniffDL **不是 BT 客户端**，没有 peer/tracker/DHT 概念。所以**不能搬协议代码**，但 EE 背后的**工程思想**——① 假源过滤、② 多源并发择优、③ 连接调度与准入、④ 失败重试/熔断、⑤ 列表热更新——与"网页嗅探多镜像下载"高度同构。下面按可迁移性给出落地方式。

| EE 思路 | SniffDL 对应场景 | 怎么移植（工程做法） |
|---|---|---|
| **① 假源过滤（正则识别坏 client → 丢弃）** | 嗅探到的候选 URL 里混着：防盗链 403、水印/低码率轨、假 m3u8、广告/引导片段、失效直链 | 维护一份 **"源特征黑名单"正则表**（像 EE 的 `peer_id`/UA 正则）：按域名/路径/UA/响应头/content-type 给候选源打分或直接剔除。命中黑名单 → **丢弃该候选、不再尝试**。等价于 EE 的 `drop_connection`。 |
| **② 多源并发择优（peer 竞速）** | 同一资源常有多清晰度/多镜像/多 CDN | **并发发起多个探测请求**（HEAD/分段 GET），择优："**首个返回正确 content-type 且可用带宽最高**"的胜出，其余**主动断开**（呼应 EE 只保留好 peer）。aria2 侧用 `split` + `max-connection-per-server` 做多连接竞速；yt-dlp 侧多 format 并发探速。这正是 BT 里"500 peer 择优、坏的断掉"的 HTTP 版。 |
| **③ 连接调度 / 准入控制（白名单、每 torrent 连接上限）** | 并发下载数、每主机连接数需要限流以免被风控/打爆 | 做 **worker 池 + 每 host 连接上限**（对应 BT 的 `MaxConnectionsPerTorrent`）；对**可信 CDN/域名走白名单优先调度**（对应 EE 的 IP 子网白名单），坏 host 降权/拉黑（对应 `peer_blacklist`）。避免 SniffDL 对单站点并发过高触发反爬封 IP。 |
| **④ 失败重试 / 熔断 / 回落（EE 屏蔽坏 peer 后回落 DHT/其它 tracker）** | 一个镜像挂了 / 一条线路被限速 | **熔断器 + 退避重试**：某 host 连续失败 N 次 → 冷却期拉黑（对应 EE 屏蔽后不再连），并**自动切换到备用源/换 UA/换线路/换 CDN**（对应"回落其它 tracker"）。重试带**指数退避 + jitter**，别死循环。 |
| **⑤ Tracker 列表热更新（订阅 URL 定时刷新）** | 站点规则 / 提取插件 / 可用镜像清单需要跟上反爬变化 | 做 **"规则/镜像清单订阅"定时热更新**：类比 ngosang/XIU2 的自动订阅——把可信 extractor/镜像源列表放远端（或跟随 yt-dlp extractor 自动更新机制），**运行时定期拉取、去重、生效**，无需重新发版。对应 EE `updateTrackersFromURL`。 |
| **⑥ GeoIP 判定（EE 用 GeoDB 判 peer 国家）** | 按地域选就近/最快 CDN、或屏蔽某地区坏源 | 引入 **IP 地理库（GeoLite2 等）**：对候选 host 做 **就近/按 ISP 选路**，或"屏蔽已知坏区域源"。等价 EE 的 `country=="CN"` 判定，但用途从"反吸血"变"择路优化"。 |
| **⑦ 队列/活跃上限（BT 队列限制同时任务）** | 批量下载时同时开太多会摊薄速度/触发风控 | SniffDL 侧做 **最大并发任务数限制 + 优先队列**（就是本文 §2B "开队列限活跃数"的通用版），集中带宽让单任务更快完成。 |

> **一句话总结移植原则**：EE 的独门不是"BT 协议魔法"，而是 **"在连接层做特征识别 + 即时丢弃 + 择优保留 + 列表热更 + 兜底切换"** 这套通用下载调度范式。SniffDL 只要把"peer"换成"候选源/host/镜像"，就能 1:1 对应到 **假源过滤、多源竞速、连接调度、失败重试、源订阅热更** 五个模块。

**落地建议（给 SniffDL 的实现优先级）**：
1. **假源过滤特征表**（①，最快见效、纯规则低风险）。
2. **多源并发择优 + 坏源即时断开**（②，直接提升成功率和速度）。
3. **失败重试 + 熔断 + 备用源切换**（④，稳定性关键）。
4. **每 host 连接上限 + 可信源白名单调度**（③，防封）。
5. **源/规则清单订阅热更新**（⑤，长期可维护）。
6. GeoIP 择路（⑥，锦上添花）。

---

## 附：主要来源清单

- EE 版本/发布/资产：[c0re100/qBittorrent-Enhanced-Edition · Releases](https://github.com/c0re100/qBittorrent-Enhanced-Edition/releases)（5.2.4.10 = "Official Update (v5.2.4)"，单一 Windows 构建；5.2.3.10 = v5.2.3 + 实验性 LT2.1）
- EE 功能清单（README 5 条）：[c0re100/qBittorrent-Enhanced-Edition · README](https://github.com/c0re100/qBittorrent-Enhanced-Edition)
- EE 私站警告 / 多实例 portable / UA / PeerID：[c0re100/qBittorrent-Enhanced-Edition · NOTE.md](https://github.com/c0re100/qBittorrent-Enhanced-Edition/blob/master/NOTE.md)
- EE 反吸血具体判定（正则/国家/端口）：`src/base/bittorrent/peer_blacklist.hpp`、`peer_filter.hpp`、`peer_filter_*_plugin.hpp`、`src/gui/{banlistoptionsdialog,ipsubnetwhitelistoptionsdialog}.cpp`（EE 仓库）
- EE Windows libtorrent 构建线（RC_2_0 / RC_1_2）：`.github/workflows/ci_windows.yaml`（EE 仓库）
- qBittorrent 参数含义与部分默认/建议（磁盘缓存、异步IO、send buffer watermark、µTP-TCP mixed 等）：[qBittorrent Wiki · Explanation of Options](https://github.com/qbittorrent/qBittorrent/wiki/Explanation-of-Options-in-qBittorrent)
- qBittorrent 各项默认值（连接 500/100、上传槽 20/4、加密 0、DHT/PeX/LSD true、AnnounceToAllTrackers false、DiskCache -1、AsyncIO 10 等）：`src/base/bittorrent/sessionimpl.cpp`（qBittorrent master 源码）
- Tracker 精选源：[ngosang/trackerslist](https://github.com/ngosang/trackerslist)
- Tracker 每日更新源（含 CDN）：[XIU2/TrackersListCollection](https://github.com/XIU2/TrackersListCollection)（`cf.trackerslist.com/all.txt`、`/best.txt`）
- libtorrent 2.x 内存/磁盘 I/O 升高的社区反馈（mmap 相关）：qBittorrent issue [#22050](https://github.com/qbittorrent/qBittorrent/issues/22050)；磁盘缓存实操数值参考社区教程 [qBittorrent 参数详细设置教程（17lai）](https://blog.17lai.site/posts/f6b32521/)（标注为社区建议，非官方）

> 本文对"机器现状"的所有描述来自对你 `D:\wws\BT\qBittorrent`、`%APPDATA%\qBittorrent`、`%LOCALAPPDATA%\qBittorrent` 的**只读检查**，未做任何写入或安装操作。
