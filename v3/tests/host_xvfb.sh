#!/bin/sh
# 起 Xvfb :77（沙箱无 root：Xvfb 从 debian 包解到 env/xvfb；二进制里 /usr/bin(xkbcomp 目录) 打补丁成 /tmp/xkb）
X=/workspace/work/env/xvfb
pgrep -f "Xvfb.p :77" >/dev/null && exit 0
mkdir -p /tmp/xkb && ln -sf $X/usr/bin/xkbcomp /tmp/xkb/xkbcomp
LD_LIBRARY_PATH=$X/usr/lib/aarch64-linux-gnu:$LD_LIBRARY_PATH nohup $X/usr/bin/Xvfb.p :77 -screen 0 1600x1000x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 2
