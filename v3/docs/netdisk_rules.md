# 网盘规则表说明（netdisk_rules.py）

> 2026-11-06 网盘直链里程碑。代码：`netdisk_rules.py`（纯标准库，可单测：`python tools/test_netdisk_rules.py`）。
> 浏览器扩展 `extension/background.js` 里的 `NETDISK_DL_HOSTS` 与本表 `dl_hosts` 保持一致，改一处要同步另一处。

## 1. 定位

SniffDL **不做网盘解析、不登录网盘**。解析交给 LinkSwift / 网盘直链下载助手 等油猴脚本，或用户在网页里点下载由扩展接管。
SniffDL 只负责把拿到的直链像 IDM 一样稳稳下完：

1. **推送方/浏览器给的请求头一字不改**（UA / Cookie / Referer 绝不覆盖，大小写不敏感去重）；
2. 只在**缺**的时候按下表补默认值（Referer / UA），Cookie 缺了就去扩展同步来的同域 Cookie 里补；
3. 解析签名直链里的到期时间，过期直接提示「链接已过期」，不拿过期链接去撞 403；
4. 403 / 412 / 404 / 429 等翻译成中文原因，写进任务列表。

## 2. 规则表（当前值）

| 规则 | 下载 CDN 域（后缀匹配） | 缺 Referer 补 | 缺 UA 补 | 需要 Cookie | 连接数 | min-split | 过期解析 |
|---|---|---|---|---|---|---|---|
| 夸克 quark | drive.quark.cn, quark.cn | https://pan.quark.cn/ | —（用推送方 UA） | 是（quark.cn） | 16 | 1M | auth_key |
| UC uc | drive.uc.cn, uc.cn | https://drive.uc.cn/ | — | 是（uc.cn） | 16 | 1M | auth_key |
| 百度 baidu | d.pcs.baidu.com, pcs.baidu.com, baidupcs.com, bcebos.com | 不补 | `pan.baidu.com` | 否 | 16 | 10M | baidu |
| 阿里云盘 aliyun | aliyundrive.net, alicloudccp.com, alipan.com, aliyundrive.com | https://www.alipan.com/ | — | 否 | 10 | 1M | OSS Expires |
| 123 云盘 | 123pan.com/.cn 及 123957/123295/123684/123865/123912/123952.com | https://yun.123pan.com/ | — | 否 | 8 | 1M | 通用 |
| 迅雷云盘 xunlei | xunlei.com, xlcdn.com, mypikpak.com | 不补 | Android Dalvik UA | 否 | 8 | 1M | 通用 |
| 蓝奏云 lanzou | lanzou[g/c/x/i/o/w/e/j/m/v/t].com, woozooo.com | https://pc.woozooo.com/ | — | 否 | 4 | 1M | 通用 |
| 115 | 115cdn.net, 115.com, 115cdn.com | https://115.com/ | — | 是（115.com） | 4 | 1M | t115 |
| 天翼云盘 189 | ctyunxs.cn, cloud.189.cn, 189.cn | https://cloud.189.cn/ | — | 否 | 8 | 1M | 通用 |

- 连接数可在 `config.json` 的 `netdisk_conn` 里按规则名覆盖，如 `{"netdisk_conn": {"115": 2}}`；不会超过全局档位和 aria2 硬顶 16。
- **补默认值永远排在推送方之后**：脚本给了 UA/Referer 就用脚本的，表里的值只在缺的时候补。

## 3. 每条规则的出处

| 项 | 出处 | 说明 |
|---|---|---|
| aria2 每服务器最大连接数 1–16、`--split`、`--min-split-size` | aria2 1.37.0 官方手册 https://aria2.github.io/manual/en/html/aria2c.html ；选项表 https://aria2.org/doc/options | 所以 `ARIA2_CONN_MAX = 16` |
| aria2 RPC 默认端口 6800、`token:SECRET` 作为第一个参数 | https://aria2.org/doc/ ；手册 `--rpc-listen-port`（默认 6800） | SniffDL 默认监听 6800、无 token |
| LinkSwift 推送格式：`aria2.addUri`，params=`["token:…", [link], {dir, out, header}]`；默认本机地址 `http://127.0.0.1:6800/jsonrpc` | 源码 https://openuserjs.org/scripts/hmjz100/LinkSwift/source （`sendLinkToAria2`）；使用文档 https://www.linkswift.cn/docs/ ；仓库 https://github.com/hmjz100/LinkSwift | `tools/netdisk_e2e.py` 的 `linkswift_addUri()` 按此一字不差组包 |
| Motrix 默认 RPC 端口 16800（与 aria2 的 6800 不同） | LinkSwift issue #506 https://github.com/hmjz100/LinkSwift/issues/506 | 用户若曾按 Motrix 配过脚本，要把端口改回 6800 |
| 百度直链（>约 20MB）必须带 `User-Agent: pan.baidu.com` | OpenList 文档 https://doc.oplist.org/guide/drivers/baidu ；AList 文档 https://alistgo.com/guide/drivers/baidu.html | 缺 UA 时补 `pan.baidu.com`；脚本给了别的 UA（如 netdisk）就用脚本的 |
| 夸克/UC 下载依赖登录 Cookie（`__pus`/`__puus`）与 Referer `https://pan.quark.cn` | AList 驱动源码 https://github.com/AlistGo/alist/blob/main/drivers/quark_uc/meta.go 、https://github.com/AlistGo/alist/blob/527ad893/drivers/quark_uc/util.go ；AList 夸克文档 https://alistgo.com/zh/guide/drivers/quark.html （「原始下载链路比较依赖请求头、账号状态」） | `need_cookie=True`，扩展同步 quark.cn / uc.cn Cookie |
| 阿里云盘直链校验 Referer（网页端登录取的链接要带阿里家的 Referer） | AList 阿里云盘文档 https://alistgo.com/zh/guide/drivers/aliyundrive.html （「由于阿里云盘 referer 的限制」）；小白羊作者说明 https://github.com/liupan1890/aliyunpan/issues/616 （`--referrer=https://www.aliyundrive.com/`）；新域名问题 https://github.com/syhyz1990/baiduyun/issues/312 | 默认补 `https://www.alipan.com/`（新域名）；脚本给了旧域名 Referer 就用脚本的 |
| 123 云盘开启了防盗链（Referer 检查） | AList issue #4712 https://github.com/AlistGo/alist/issues/4712 ；OpenList 123 文档 https://doc.oplist.org.cn/guide/drivers/123 | 默认补 `https://yun.123pan.com/` |
| 迅雷 / 蓝奏 / 115 / 天翼 的 Referer、UA、连接数 | **经验值，未找到权威出处，待真机实测** | 推送方给的头优先，这些默认值只在缺头时才生效；115 连接数保守取 4 |
| 各规则连接数（16/10/8/4） | **工程经验值**（不是网盘官方数字） | 403/429 多时可用 `netdisk_conn` 调低 |

## 4. 失败原因翻译（写进任务列表）

| 现象 | 显示给用户（`explain_failure()`，节选） |
|---|---|
| 签名参数已到期（auth_key / Expires / 百度等） | 链接已过期（签名到期 xx-xx xx:xx），请回网盘页面重新点下载 / 让脚本重新解析后再推送 |
| 412 且没带 Cookie | 缺登录 Cookie（HTTP 412）：该站要求登录态……请确认网盘网页已登录，用浏览器扩展接管或让脚本携带 Cookie |
| 412 且带了 Cookie | 登录 Cookie 无效或与取链时不一致（HTTP 412） |
| 403 百度 | UA 不对或链接已过期——百度直链必须用解析工具指定的 UA（附当前 UA） |
| 403 需 Cookie 的盘且没带 | 被拒（HTTP 403）：缺登录 Cookie |
| 403 其它 | 链接可能已过期，或 UA/Cookie/IP 与取链时不一致 |
| 401 | 需要鉴权 |
| 404 / 410 | 文件不存在或链接已失效 |
| 429 / 503 / 509 | 服务器限流，可在 config.json 的 netdisk_conn 调小该网盘连接数 |
| 416 | 服务器不接受分段请求，点继续将从头下载 |
| 超时 / DNS 失败 | 连接超时 / 域名解析失败 |

每条后面会追加该网盘规则的 `tips`。

## 5. 怎么加一个新网盘

1. 在 `PROFILES` 里加一行（下载 CDN 域、Referer、UA、Cookie 域、连接数）；
2. 同步扩展 `background.js` 的 `NETDISK_DL_HOSTS`（以及网盘网页域、Cookie 同步域）；
3. `tools/test_netdisk_rules.py` 加一条匹配用例；
4. 跑 `python tools/test_netdisk_rules.py`、`python tools/netdisk_e2e.py`、`node tools/ext_netdisk_regression.js`，全绿再提交；
5. 在本文件第 3 节补上出处。
