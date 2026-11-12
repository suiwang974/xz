# -*- coding: utf-8 -*-
"""SniffDL v3 内核（无界面后台服务）。

模块：
  headless   无 Tk 的调度器（替代 root.after）与变量桩
  events     事件总线 + SSE 推送
  settings   设置表（默认值/类型/分组/校验），界面「常用 / 高级」按它渲染
  naming     自动分类、文件名模板、重名策略
  batch      批量链接展开（file[001-100].jpg / [a-z]）
  checksum   下载后校验 md5/sha1/sha256
  ratelimit  令牌桶限速（全局 + 单任务）
  queues     多队列 / 同时下载数 / 时间段计划 / 全速时段
  postactions 完成后动作（打开文件/文件夹/运行命令/关机/休眠/退出）
  clipboard  剪贴板监视（宿主上报 + Windows 原生兜底）
  bencode    .torrent 解析（选文件下载）
  recognize  一个框识别一切：直链/视频站/m3u8/种子/磁力/网盘/批量
  platform_util 打开文件/文件夹、开机自启、关机命令（跨平台）
  runtime    运行时状态文件 + stdout 握手（界面拿真实端口/令牌）
  engine     Engine：复用 video_downloader.App 的下载/嗅探/BT 引擎方法，但不建任何窗口
  api        sniffdl.* 新接口注册表（扩展点：register(method, fn, ui=True)）
"""
