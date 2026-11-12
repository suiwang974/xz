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
