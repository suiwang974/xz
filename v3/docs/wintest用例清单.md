# wintest 用例清单（12-01 后在 GitHub Actions Windows 跑）
1. POST 接管：浏览器表单 POST 下载经插件交给 SniffDL，文件哈希一致。
2. 嗅探：webRequest 捕获 m3u8/mp4，面板出现条目并可一键下载。
3. Native Messaging com.sniffdl.host：插件一键拉起 SniffDL 并返回版本。
4. aria2 RPC 127.0.0.1:6800 无 token：addUri/tellStatus/changeGlobalOption 生效。
5. 双限速：BT 与直链同跑时全局限速按比例分摊。
6. thunder/flashget/qqdl 链接解码后可下载。
7. 多屏桌宠覆盖全部屏幕；托盘关闭/暂停 BT。
8. 统计页/日志页可打开，深浅色正常。
9. 浏览器插件：POST 表单下载被接管并带 Cookie/Referer 成功。
10. 插件 webRequest 嗅探到视频/音频流，列表可一键发送到 SniffDL。
11. Native Messaging com.sniffdl.host 能从插件拉起 SniffDL。
12. 磁盘满/权限不足时给出中文错误，不崩溃。
13. HLS/DASH 内置下载（11-13 新增）：本地假 m3u8（明文 + AES-128）与 DASH 点播由内核 core/hls.py 下完并合并，sha 一致；无 ffmpeg 时 DASH 音视频分文件保留并如实提示；直播流回落旧路径。对应沙箱 tests/hls_e2e.py。
14. aria2 WebSocket 接口（11-13 新增）：ws://127.0.0.1:6800/jsonrpc 能收到 onDownloadStart/Complete/Error 通知，坏 Origin 被拒，AriaNg 之类客户端可连。对应 tests/rpc_ws_e2e.py。
15. 插件自动拉起内核（11-13 新增）：内核没开时，点插件「推送/接管」经 Native Messaging 拉起 SniffDL 后重试成功；60 秒内拉起失败不重复试。对应 tests/ext_autolaunch.py。
16. popup 嗅探列表（11-13 新增）：弹窗里按页面分组、显示大小、单条/全部下载、清空；深浅色正常。对应 tests/ext_sniff.py。
17. 主窗关闭按钮读设置（11-13 新增）：设置里「关闭到托盘」关→点关闭直接退出，开→缩到托盘并保留下载。对应 tests/host_tray_test.py（Windows 托盘钩子待 CI 实测）。
18. 磁力链接按 bt_exclude 排除广告文件（11-13 新增）：元数据到手后自动只选正片。对应 core_e2e magnet 组。
19. 外挂引擎与提示音（11-13 新增）：设置里填 N_m3u8DL-RE / BBDown 路径后 m3u8、B 站链接走外挂，失败回落内置；任务完成响提示音；设置页「工具与诊断」可打开保存目录/日志、运行自检。对应 tests/sidecar_test.py、ui_sound.py、ui_tools.py。
20. 网盘直链 v3 子集（11-13 新增）：mock 网盘上 LinkSwift/panlinker 组包推送、缺 Cookie 报 412 原因、过期链接报「过期」、粘贴直链自动补同步 Cookie。对应 tools/netdisk_e2e.py --v3（Windows 上同命令复跑）。
21. POST 接管补强（11-14 新增）：带 Cookie/Referer 的 POST、308 重定向后仍 POST、Content-Disposition 引号含分号/inline filename*。对应 tests/post_e2e.py hdr 组。
22. 计划任务真触发（11-14 新增）：计划队列窗口外不开工、开启自动下载、关闭暂停、再开续完 sha 一致。对应 tests/resume_edge_e2e.py sched 组。
23. 嗅探分类与去重（11-14 新增）：m3u8/mpd/视频/音频分类，图片/json 不误收，同地址只记一条。对应 tests/ext_sniff_classify.py。
