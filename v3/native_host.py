#!/usr/bin/env python3
"""Native Messaging host com.sniffdl.host：ping / launch（内核没开就拉起，已在跑不重复起）。"""
import json, os, socket, struct, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = "com.sniffdl.host"

def read_msg(f=None):
    f = f or sys.stdin.buffer
    raw = f.read(4)
    if len(raw) < 4:
        return None
    n = struct.unpack("<I", raw)[0]
    return json.loads(f.read(n).decode("utf-8"))

def write_msg(obj, f=None):
    f = f or sys.stdout.buffer
    b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    f.write(struct.pack("<I", len(b)) + b); f.flush()

def port_open(port, host="127.0.0.1"):
    s = socket.socket(); s.settimeout(0.5)
    try:
        s.connect((host, port)); return True
    except OSError:
        return False
    finally:
        s.close()

def launch(port=6800):
    if port_open(port):
        return dict(ok=True, started=False, running=True)
    cmd = os.environ.get("SNIFFDL_LAUNCH_CMD")
    args = cmd.split() if cmd else [sys.executable, os.path.join(HERE, "sniffdl_core.py"), "--port", str(port)]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x08000000
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=HERE, **kw)
    import time
    for _ in range(40):
        if port_open(port):
            return dict(ok=True, started=True, running=True)
        time.sleep(0.25)
    return dict(ok=False, started=True, running=False, error="内核启动超时")

def handle(m):
    a = (m or {}).get("action")
    port = int((m or {}).get("port") or 6800)
    if a == "ping":
        return dict(ok=True, host=HOST, running=port_open(port))
    if a == "launch":
        return launch(port)
    return dict(ok=False, error="未知指令")

def make_manifest(ext_ids, path=None):
    path = path or os.path.abspath(__file__)
    return {"name": HOST, "description": "SniffDL 一键拉起", "path": path, "type": "stdio",
            "allowed_origins": ["chrome-extension://%s/" % i for i in ext_ids]}

def install(ext_ids, dirs):
    out = []
    for d in dirs:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, HOST + ".json")
        json.dump(make_manifest(ext_ids), open(p, "w"), ensure_ascii=False, indent=2)
        out.append(p)
    return out

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--install":
        print(install(sys.argv[2].split(","), sys.argv[3:] or [os.path.expanduser("~/.config/google-chrome/NativeMessagingHosts")]))
    else:
        while True:
            m = read_msg()
            if m is None:
                break
            write_msg(handle(m))
