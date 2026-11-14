# wintest 用例清单 ↔ 沙箱测试 对账（11-14）
「沙箱」= 本沙箱（Linux）已跑通的测试；「CI」= 已写进 pubrepo/.github/workflows/wintest.yml 的 Windows 步骤。Windows 未实测，不得宣称已测。

| 用例 | 沙箱测试 | 是否已进 CI |
|---|---|---|
| 1/9/21 POST 接管 | tests/post_e2e.py、ext_post_e2e.py、ext_takeover_test.py | 未进（待加） |
| 2/10/16/23 嗅探 | tests/ext_sniff.py、ext_sniff_classify.py、ext_hover.py | 经 win_ext 部分覆盖 |
| 3/11/15 Native Messaging | tests/ext_native.py、ext_autolaunch.py | 经 win_ext 部分覆盖 |
| 4/14 aria2 RPC / WebSocket | tests/rpc_compat_e2e.py、rpc_ws_e2e.py | 未进（待加） |
| 5 双限速 | tests/bothlimit_test.py | 未进 |
| 6 迅雷/快车/QQ 链接 | tests/linkdecode_test.py、linkdecode2_test.py | 未进 |
| 7/17 多屏桌宠/托盘 | tests/host_tray_test.py、host_electron_test.py | win_host |
| 8 统计/日志页深浅色 | tests/ui_smoke.py、ui_pages.py | ui_smoke 已进 |
| 12 磁盘满 | tests/diskfull_test.py、core_e2e diskfull 组（Linux unshare 专用） | 未进（Windows 需另写） |
| 13 HLS/DASH | tests/hls_e2e.py、hls_test.py | 未进 |
| 18 磁力排除 | core_e2e magnet 组、btexclude_test.py | 未进 |
| 19 外挂引擎/提示音 | tests/sidecar_test.py、ui_sound.py、ui_tools.py | 未进 |
| 20 网盘 v3 | tools/netdisk_e2e.py --v3 | netdisk_e2e 已进 |
| 22 计划任务 | tests/resume_edge_e2e.py sched 组 | 未进 |
