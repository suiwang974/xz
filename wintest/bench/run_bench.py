# -*- coding: utf-8 -*-
"""SniffDL v3 与 IDM / aria2 同条件对比测速。

  python run_bench.py [--scale full|small] [--runs 3] [--tools sdl,aria2,idm] [--scenarios A,B,C,D,E,R1,R2]
                      [--v3 ../../v3] [--aria2 aria2c] [--idm "C:\\...\\IDMan.exe"] [--out 结果目录]
  python run_bench.py --report-only --out 结果目录      # 只用 bench_result.json 重新生成 测速报告.md

场景（本地服务器 server.py 一次起好，全部场景同一进程）：
  A 大文件不限速   B 单连接限速 2 MB/s   C 每请求首字节 +200 ms、每 64 KB 抖动 0~20 ms
  D 大量小文件（500 × 200 KB）   E 服务器最多 4 个并发传输，超出 503（每连接 5 MB/s）
  R1 / R2 真实公网测速文件（proof.ovh.net 100 MB / ash-speed.hetzner.com 100 MB）
工具：
  sdl    SniffDL v3 内核 core.speed.run_http → 自研多连接引擎 sdl_speed（sdl_cli.py；设置全默认：最大连接数=自动）
  aria2  aria2c -x16 -s16 -k1M（另加 --retry-wait=1 让 503 能重试；小文件 -j 并发同 --parallel）
  idm    IDMan.exe /d URL /p 目录 /f 文件名 /n /a 加入队列 → IDMan.exe /s 开始；轮询文件大小判断完成。
         测前写注册表 HKCU\\Software\\DownloadManager：MaxConnectionsNumber=32，关闭开始/完成对话框和更新检查。
每个场景 × 每个工具跑 --runs 次，记录耗时、平均 MB/s、sha256。工具装不上/跑不了 → 记「未测」+ 原因，绝不编数字。
计时：sdl 用内核内部计时（从发起下载到文件落到最终名，不含 Python 解释器启动；真实 App 里内核常驻）；
aria2 用进程墙钟（含其启动，约十几毫秒）；IDM 从 /s 开始到最后一个文件大小达标且可独占打开为止。
输出：<out>/bench_result.json、<out>/测速报告.md
"""
import argparse
import datetime
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
MB = 1024 * 1024
IS_WIN = os.name == "nt"

SCEN_DESC = {
    "A": "A 大文件不限速",
    "B": "B 单连接限速 2 MB/s（网盘式）",
    "C": "C 高延迟（首字节 +200 ms，每 64 KB 抖动 0~20 ms）",
    "D": "D 大量小文件",
    "E": "E 服务器最多 4 并发，超出 503",
    "R1": "R1 公网 proof.ovh.net 100 MB",
    "R2": "R2 公网 ash-speed.hetzner.com 100 MB",
}
REAL = {
    "R1": "https://proof.ovh.net/files/100Mb.dat",
    "R2": "https://ash-speed.hetzner.com/100MB.bin",
}
LEAD_REQUIRED = ("B", "C", "D")       # 用户验收：高延迟、单连接限速、大量小文件三类要领先
TOOL_NAME = {"sdl": "SniffDL v3", "aria2": "aria2c -x16 -s16", "idm": "IDM"}


def log(*a):
    print("[bench]", *a, flush=True)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(4 * MB), b""):
            h.update(b)
    return h.hexdigest()


def http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "bench"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def head_size(url, timeout=20):
    """公网文件大小（给 IDM 轮询判断完成用）。读环境代理。"""
    for meth in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=meth, headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-0"}
                                         if meth == "GET" else {"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                cr = r.headers.get("Content-Range")
                if cr and "/" in cr:
                    return int(cr.rsplit("/", 1)[1])
                cl = r.headers.get("Content-Length")
                if cl and meth == "HEAD":
                    return int(cl)
        except Exception:
            continue
    # urllib 过某些代理会失败：退到 curl（Windows 10+ 自带 curl.exe）
    cu = shutil.which("curl") or shutil.which("curl.exe")
    if cu:
        try:
            p = subprocess.run([cu, "-sS", "-m", str(timeout), "-o", os.devnull, "-D", "-", "-r", "0-0", "-L", url],
                               capture_output=True, text=True, timeout=timeout + 5)
            for ln in p.stdout.splitlines():
                if ln.lower().startswith("content-range:") and "/" in ln:
                    return int(ln.rsplit("/", 1)[1].strip())
        except Exception:
            pass
    return None


# ------------------------------------------------------------------ 服务器
class LocalServer:
    def __init__(self, data_dir, scale, extra=()):
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py"), "--data", data_dir,
                                   "--scale", scale] + list(extra),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        t0 = time.time()
        line = ""
        while time.time() - t0 < 900:
            line = self.p.stdout.readline()
            if line.startswith("READY"):
                break
            if not line and self.p.poll() is not None:
                raise RuntimeError("server.py 启动失败")
        self.port = int(line.split()[1])
        self.base = "http://127.0.0.1:%d" % self.port
        self.manifest = http_json(self.base + "/manifest.json")
        self.cfg = json.load(open(os.path.join(data_dir, "server_cfg.json"), encoding="utf-8"))

    def stats(self):
        try:
            return http_json(self.base + "/stats.json")
        except Exception:
            return {}

    def reset(self):
        try:
            http_json(self.base + "/stats/reset")
        except Exception:
            pass

    def stop(self):
        try:
            self.p.terminate()
            self.p.wait(10)
        except Exception:
            pass


# ------------------------------------------------------------------ 工具驱动
class ToolUnavailable(Exception):
    pass


class SdlTool:
    key = "sdl"

    def __init__(self, a):
        self.v3 = os.path.abspath(a.v3)
        self.parallel = a.parallel
        if not os.path.isdir(os.path.join(self.v3, "sdl_speed")):
            raise ToolUnavailable("找不到 %s/sdl_speed" % self.v3)
        self.version = "v3 @ %s" % self.v3

    def run(self, urls, outdir, timeout):
        lst = os.path.join(outdir, "_urls.txt")
        os.makedirs(outdir, exist_ok=True)
        with open(lst, "w", encoding="utf-8") as f:
            f.write("\n".join(urls))
        cmd = [sys.executable, os.path.join(HERE, "sdl_cli.py"), "--v3", self.v3, "--out", outdir,
               "--parallel", str(self.parallel), "--list", lst]
        t0 = time.perf_counter()
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        wall = time.perf_counter() - t0
        os.remove(lst)
        last = (p.stdout.strip().splitlines() or ["{}"])[-1]
        try:
            j = json.loads(last)
        except Exception:
            return {"ok": False, "error": "sdl_cli 输出无法解析：%s %s" % (last[:200], p.stderr[-400:]), "wall": wall}
        res = {"ok": j.get("fail", 1) == 0, "elapsed": j.get("elapsed") or wall, "wall": round(wall, 3),
               "detail": "; ".join(j.get("logs") or [])[:300]}
        if j.get("errors"):
            res["error"] = " | ".join(j["errors"][:3])
        return res


class Aria2Tool:
    key = "aria2"
    BASE = ["-x16", "-s16", "-k1M"]

    def __init__(self, a):
        exe = a.aria2 or shutil.which("aria2c") or shutil.which("aria2c.exe")
        if not exe or not os.path.exists(exe):
            raise ToolUnavailable("找不到 aria2c（--aria2 或 PATH）")
        self.exe = exe
        self.parallel = a.parallel
        try:
            self.version = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                          timeout=20).stdout.splitlines()[0]
        except Exception as e:
            raise ToolUnavailable("aria2c --version 失败：%s" % e)

    def run(self, urls, outdir, timeout):
        os.makedirs(outdir, exist_ok=True)
        lst = os.path.join(outdir, "_urls.txt")
        with open(lst, "w", encoding="utf-8") as f:
            f.write("\n".join(urls) + "\n")
        cmd = [self.exe] + self.BASE + ["-d", outdir, "-i", lst, "-j", str(self.parallel),
                                        "--retry-wait=1", "--max-tries=20", "--allow-overwrite=true",
                                        "--auto-file-renaming=false", "--console-log-level=warn",
                                        "--summary-interval=0", "--download-result=hide",
                                        "--check-certificate=true", "--user-agent=Mozilla/5.0 aria2-bench"]
        if all(u.startswith("http://127.0.0.1") for u in urls):
            cmd.append("--no-proxy=127.0.0.1,localhost")
        t0 = time.perf_counter()
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        el = time.perf_counter() - t0
        os.remove(lst)
        res = {"ok": p.returncode == 0, "elapsed": el, "wall": round(el, 3)}
        if p.returncode != 0:
            res["error"] = "退出码 %d：%s" % (p.returncode, (p.stdout + p.stderr).strip()[-300:])
        return res


class IdmTool:
    key = "idm"
    REG = {  # HKCU\Software\DownloadManager（DWORD）
        "MaxConnectionsNumber": 32,   # 默认 8 → 32（IDM 界面最高档）
        "StartDlgShowing": 0,         # 不弹「下载文件信息」
        "ComplDlgShowing": 0,         # 不弹「下载完成」
        "CheckUpdtVM": 0,             # 不检查更新
        "LaunchOnStart": 0,
        "TipStartUp": 0,
        "ShowTipOnFirstCatch": 0,
        "DontShowDlgOnCancel": 1,
        "MonitorUrlClipboard": 0,
    }

    def __init__(self, a):
        if not IS_WIN:
            raise ToolUnavailable("IDM 只有 Windows 版（当前 %s）" % platform.system())
        exe = a.idm or self._find()
        if not exe or not os.path.exists(exe):
            raise ToolUnavailable("没找到 IDMan.exe（安装失败或未安装，见安装步骤日志）")
        self.exe = exe
        self.version = self._ver()
        self.parallel = a.parallel
        self.shots = a.out
        self._configure()

    @staticmethod
    def _find():
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\DownloadManager")
            p = winreg.QueryValueEx(k, "ExePath")[0]
            if p and os.path.exists(p):
                return p
        except Exception:
            pass
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
            if base:
                p = os.path.join(base, "Internet Download Manager", "IDMan.exe")
                if os.path.exists(p):
                    return p
        return None

    def _ver(self):
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\DownloadManager")
            return "IDM " + str(winreg.QueryValueEx(k, "idmvers")[0])
        except Exception:
            return "IDM（版本未读到）"

    def _kill(self):
        subprocess.run(["taskkill", "/F", "/IM", "IDMan.exe"], capture_output=True)
        time.sleep(1.5)

    def _configure(self):
        import winreg
        self._kill()
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\DownloadManager")
        for n, v in self.REG.items():
            winreg.SetValueEx(k, n, 0, winreg.REG_DWORD, int(v))
        winreg.CloseKey(k)
        subprocess.Popen([self.exe])            # 先起常驻进程，后面的 /d /s 交给它
        time.sleep(6)
        self.screenshot("idm_started")

    def screenshot(self, tag):
        try:
            from PIL import ImageGrab
            p = os.path.join(self.shots, "shot_%s_%d.png" % (tag, int(time.time())))
            ImageGrab.grab().save(p)
            return p
        except Exception:
            return None

    def _idm(self, *args, timeout=60):
        return subprocess.run([self.exe] + list(args), capture_output=True, timeout=timeout)

    @staticmethod
    def _complete(path, size):
        try:
            if os.path.getsize(path) != size:
                return False
            with open(path, "r+b"):
                return True
        except OSError:
            return False

    def run(self, urls, outdir, timeout, sizes=None, run_tag=""):
        os.makedirs(outdir, exist_ok=True)
        names = []
        for u in urls:
            name = os.path.basename(u.split("?")[0])
            names.append(name)
            uu = u + ("&" if "?" in u else "?") + "bench=%s" % run_tag   # 每轮不同 URL，避免 IDM 重复下载提示
            self._idm("/d", uu, "/p", outdir, "/f", name, "/n", "/a")
        t0 = time.perf_counter()
        self._idm("/s")
        pending = set(range(len(names)))
        last_change = time.perf_counter()
        last_total = -1
        while pending:
            for i in list(pending):
                if sizes and sizes[i] and self._complete(os.path.join(outdir, names[i]), sizes[i]):
                    pending.discard(i)
            if not pending:
                break
            now = time.perf_counter()
            tot = sum(os.path.getsize(os.path.join(outdir, n)) for n in names
                      if os.path.exists(os.path.join(outdir, n)))
            if tot != last_total:
                last_total, last_change = tot, now
            if now - t0 > timeout:
                shot = self.screenshot("idm_timeout")
                return {"ok": False, "elapsed": now - t0, "error": "超时 %ds，还剩 %d 个文件未完成%s" % (
                    timeout, len(pending), "（截图 %s）" % os.path.basename(shot) if shot else "")}
            time.sleep(0.05 if len(names) > 1 or (sizes and sizes[0] < 300 * MB) else 0.2)
        el = time.perf_counter() - t0
        return {"ok": True, "elapsed": el, "wall": round(el, 3)}


# ------------------------------------------------------------------ 场景
def build_scenarios(srv, wanted):
    sc = {}
    m = srv.manifest if srv else {}
    for key in ("A", "B", "C", "E"):
        if key in wanted and srv:
            rel = "%s/big.bin" % key
            sc[key] = {"urls": [srv.base + "/" + rel], "files": {"big.bin": m[rel]},
                       "bytes": m[rel]["size"]}
    if "D" in wanted and srv:
        rels = sorted(k for k in m if k.startswith("D/"))
        sc["D"] = {"urls": [srv.base + "/" + r for r in rels],
                   "files": {os.path.basename(r): m[r] for r in rels},
                   "bytes": sum(m[r]["size"] for r in rels)}
    for key, url in REAL.items():
        if key in wanted:
            size = head_size(url)
            sc[key] = {"urls": [url], "files": {os.path.basename(url): {"size": size, "sha256": None}},
                       "bytes": size, "real": True}
    if srv:
        cfg = srv.cfg
        if "A" in sc:
            sc["A"]["desc"] = "A 大文件 %d MB，不限速" % (cfg["a_size"] // MB)
        if "B" in sc:
            sc["B"]["desc"] = "B %d MB，单连接限速 %.1f MB/s（网盘式）" % (cfg["b_size"] // MB, cfg["b_rate"] / MB)
        if "C" in sc:
            sc["C"]["desc"] = "C %d MB，首字节 +%d ms、每 64 KB 抖动 0~%d ms" % (
                cfg["c_size"] // MB, cfg["c_latency"] * 1000, cfg["c_jitter"] * 1000)
        if "D" in sc:
            sc["D"]["desc"] = "D %d 个 × %d KB 小文件" % (cfg["d_count"], cfg["d_size"] // 1024)
        if "E" in sc:
            sc["E"]["desc"] = "E %d MB，服务器最多 %d 并发、超出 503，每连接 %.0f MB/s" % (
                cfg["e_size"] // MB, cfg["e_max_conn"], cfg["e_rate"] / MB)
    for k, v in sc.items():
        v.setdefault("desc", SCEN_DESC.get(k, k))
    return sc


def verify(outdir, files):
    """返回 (全部一致?, 说明, {name: sha})。公网文件没有官方 sha → 只记哈希，事后跨工具比对。"""
    bad, got = [], {}
    for name, meta in files.items():
        p = os.path.join(outdir, name)
        if not os.path.exists(p):
            bad.append("%s 缺失" % name)
            continue
        if meta.get("size") and os.path.getsize(p) != meta["size"]:
            bad.append("%s 大小 %d≠%d" % (name, os.path.getsize(p), meta["size"]))
            continue
        h = sha256_file(p)
        got[name] = h
        if meta.get("sha256") and h != meta["sha256"]:
            bad.append("%s sha256 不符" % name)
    return (not bad), "; ".join(bad[:5]), got


def run_all(a):
    os.makedirs(a.out, exist_ok=True)
    wanted = [s.strip().upper() for s in a.scenarios.split(",") if s.strip()]
    tools_wanted = [t.strip().lower() for t in a.tools.split(",") if t.strip()]
    work = a.work or os.path.join(tempfile.gettempdir(), "sdl_bench_work")
    data = a.data or os.path.join(tempfile.gettempdir(), "sdl_bench_data")
    result = {"generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
              "host": {"os": platform.platform(), "python": sys.version.split()[0], "cpus": os.cpu_count(),
                       "machine": platform.machine(), "ci": bool(os.environ.get("GITHUB_ACTIONS"))},
              "scale": a.scale, "runs": a.runs, "parallel": a.parallel, "sandbox": a.sandbox_note,
              "tools": {}, "scenarios": {}}
    tools = {}
    for t in tools_wanted:
        cls = {"sdl": SdlTool, "aria2": Aria2Tool, "idm": IdmTool}.get(t)
        if not cls:
            continue
        try:
            tools[t] = cls(a)
            result["tools"][t] = {"status": "ok", "version": getattr(tools[t], "version", "")}
            log("工具就绪", t, result["tools"][t]["version"])
        except ToolUnavailable as e:
            result["tools"][t] = {"status": "未测", "reason": str(e)}
            log("工具不可用", t, e)
        except Exception as e:  # noqa: BLE001
            result["tools"][t] = {"status": "未测", "reason": "%s: %s" % (type(e).__name__, e)}
            log("工具初始化异常", t, e)
    if a.idm_install_note and "idm" in result["tools"]:
        result["tools"]["idm"]["install"] = a.idm_install_note

    srv = None
    local = [s for s in wanted if s in "ABCDE"]
    if local:
        extra = []
        if a.server_args:
            extra = a.server_args.split()
        log("生成测试数据 / 启动服务器 …", data)
        srv = LocalServer(data, a.scale, extra)
        result["server"] = srv.cfg
    try:
        scen = build_scenarios(srv, wanted)
        for key in wanted:
            if key not in scen:
                continue
            s = scen[key]
            ent = {"desc": s["desc"], "bytes": s["bytes"], "files": len(s["files"]), "tools": {}}
            result["scenarios"][key] = ent
            if s.get("real") and not s["bytes"]:
                for t in tools_wanted:
                    ent["tools"][t] = {"status": "未测", "reason": "公网文件拿不到大小（网络不可达）"}
                continue
            active = []
            for t in tools_wanted:
                if t not in tools:
                    ent["tools"][t] = {"status": "未测", "reason": result["tools"].get(t, {}).get("reason", "未启用")}
                else:
                    active.append(t)
            allruns = {t: [] for t in active}
            # 轮流跑（第 1 轮 sdl→aria2→idm，第 2 轮 …），避免机器状态漂移只偏向某一个工具
            for r in range(1, a.runs + 1):
                for t in active:
                    tool = tools[t]
                    outdir = os.path.join(work, "%s_%s_%d" % (key, t, r))
                    shutil.rmtree(outdir, ignore_errors=True)
                    if srv:
                        srv.reset()
                    timeout = a.timeout or max(180, int(s["bytes"] / MB * 3))
                    try:
                        if t == "idm":
                            sizes = [s["files"][os.path.basename(u.split("?")[0])]["size"] for u in s["urls"]]
                            res = tool.run(s["urls"], outdir, timeout, sizes=sizes,
                                           run_tag="%s%d%d" % (key, r, int(time.time())))
                        else:
                            res = tool.run(s["urls"], outdir, timeout)
                    except subprocess.TimeoutExpired:
                        res = {"ok": False, "error": "超时 %ds" % timeout}
                    except Exception as e:  # noqa: BLE001
                        res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
                    if res.get("ok"):
                        okv, why, hashes = verify(outdir, s["files"])
                        res["sha_ok"] = okv
                        if not okv:
                            res["ok"] = False
                            res["error"] = "校验失败：" + why
                        if s.get("real"):
                            res["sha256"] = hashes.get(next(iter(s["files"])))
                        el = max(1e-6, float(res["elapsed"]))
                        res["mbps"] = round(s["bytes"] / MB / el, 2)
                        res["elapsed"] = round(el, 3)
                    if srv and key in "ABCDE":
                        st = srv.stats().get(key, {})
                        res["server"] = {k: st.get(k) for k in ("requests", "connections", "peak_active",
                                                                  "rejected_503")}
                    allruns[t].append(res)
                    log(key, t, "第%d次" % r, "%.2f MB/s" % res["mbps"] if res.get("mbps") and res.get("ok") else
                        "失败 " + str(res.get("error"))[:200])
                    shutil.rmtree(outdir, ignore_errors=True)
            for t in active:
                runs = allruns[t]
                good = [x for x in runs if x.get("ok")]
                te = {"runs": runs}
                if good:
                    te["status"] = "ok" if len(good) == len(runs) else "部分失败"
                    te["avg_mbps"] = round(statistics.mean(x["mbps"] for x in good), 2)
                    te["avg_elapsed"] = round(statistics.mean(x["elapsed"] for x in good), 3)
                    te["ok_runs"] = len(good)
                else:
                    te["status"] = "失败"
                    te["reason"] = (runs[-1].get("error") if runs else "无结果") or "未知"
                ent["tools"][t] = te
            # 公网：跨工具 sha256 比对
            if s.get("real"):
                hs = {}
                for t, te in ent["tools"].items():
                    for x in te.get("runs", []):
                        if x.get("sha256"):
                            hs.setdefault(x["sha256"], []).append(t)
                if hs:
                    major = max(hs, key=lambda h: len(hs[h]))
                    ent["sha256_consensus"] = major
                    ent["sha256_variants"] = len(hs)
            json.dump(result, open(os.path.join(a.out, "bench_result.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
    finally:
        if srv:
            srv.stop()
    json.dump(result, open(os.path.join(a.out, "bench_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return result


# ------------------------------------------------------------------ 报告
def _fmt(v, nd=2):
    return ("%%.%df" % nd) % v if isinstance(v, (int, float)) else "—"


def evaluate(result):
    rows, ratios_idm, verdicts = [], [], {}
    for key, ent in result["scenarios"].items():
        tl = ent["tools"]
        sp = {t: tl.get(t, {}).get("avg_mbps") for t in ("sdl", "aria2", "idm")}
        s, ar, idm = sp["sdl"], sp["aria2"], sp["idm"]
        v = {"vs_idm": None, "vs_aria2": None, "pass": None, "why": []}
        if s is not None and idm:
            v["vs_idm"] = s / idm - 1
            ratios_idm.append(s / idm)
        if s is not None and ar:
            v["vs_aria2"] = s / ar - 1
        if s is None:
            v["why"].append("SniffDL 无结果")
        else:
            need_lead = key in LEAD_REQUIRED
            conds = []
            if idm:
                conds.append(s >= idm * 1.2 if not need_lead else (s >= idm * 1.2))
            else:
                v["why"].append("IDM 未测")
            if ar:
                conds.append(s > ar if need_lead else s >= ar)
            else:
                v["why"].append("aria2 未测")
            if conds:
                v["pass"] = all(conds)
        verdicts[key] = v
    overall = None
    if ratios_idm:
        overall = statistics.mean(ratios_idm) - 1
    return verdicts, overall


def write_report(result, out):
    verdicts, overall = evaluate(result)
    L = []
    sb = result.get("sandbox")
    L.append("# SniffDL v3 对比测速报告（IDM / aria2 同条件）")
    L.append("")
    if sb:
        L.append("> ⚠️ %s" % sb)
        L.append("")
    h = result["host"]
    L.append("- 生成时间：%s" % result["generated"])
    L.append("- 机器：%s，%s 核，Python %s%s" % (h["os"], h["cpus"], h["python"], "（GitHub Actions）" if h.get("ci") else ""))
    L.append("- 规模：%s；每个场景 × 工具跑 %d 次取平均；小文件同时下载数统一为 %d" % (
        result["scale"], result["runs"], result["parallel"]))
    L.append("- 工具：")
    for t in ("sdl", "aria2", "idm"):
        ti = result["tools"].get(t)
        if not ti:
            continue
        if ti["status"] == "ok":
            L.append("  - %s：%s%s" % (TOOL_NAME[t], ti.get("version", ""),
                                      "；安装：%s" % ti["install"] if ti.get("install") else ""))
        else:
            L.append("  - %s：**未测** —— %s%s" % (TOOL_NAME[t], ti.get("reason", ""),
                                               "；安装：%s" % ti["install"] if ti.get("install") else ""))
    L.append("- 条件：SniffDL 走内核 core.speed.run_http（设置默认，最大连接数=自动 8→64）；aria2c `-x16 -s16 -k1M --retry-wait=1`；"
             "IDM 注册表 MaxConnectionsNumber=32、关闭开始/完成对话框，命令行 `/d … /n /a` 入队后 `/s` 开始。")
    L.append("")
    L.append("## 结果（平均 MB/s，括号内为相对 IDM）")
    L.append("")
    L.append("| 场景 | SniffDL | aria2 -x16 | IDM | SniffDL 相对 IDM | SniffDL 相对 aria2 | 达标 |")
    L.append("|---|---:|---:|---:|---:|---:|:---:|")
    for key, ent in result["scenarios"].items():
        tl = ent["tools"]
        cells = []
        for t in ("sdl", "aria2", "idm"):
            te = tl.get(t, {})
            if te.get("avg_mbps") is not None:
                c = _fmt(te["avg_mbps"])
                if te.get("status") == "部分失败":
                    c += "（%d/%d 次成功）" % (te.get("ok_runs", 0), len(te.get("runs", [])))
            elif te.get("status") in ("未测", None):
                c = "未测"
            else:
                c = "失败"
            cells.append(c)
        v = verdicts[key]
        vi = ("%+.1f%%" % (v["vs_idm"] * 100)) if v["vs_idm"] is not None else "—"
        va = ("%+.1f%%" % (v["vs_aria2"] * 100)) if v["vs_aria2"] is not None else "—"
        if v["pass"] is None:
            ok = "—"
        else:
            ok = "✅" if v["pass"] else "❌"
        if v["why"] and v["pass"] is not None:
            ok += "（%s）" % "、".join(v["why"])
        elif v["why"]:
            ok = "无法判定（%s）" % "、".join(v["why"])
        lead = "（需领先）" if key in LEAD_REQUIRED else ""
        L.append("| %s%s | %s | %s | %s | %s | %s | %s |" % (ent["desc"], lead, cells[0], cells[1], cells[2], vi, va, ok))
    L.append("")
    L.append("## 验收（用户目标）")
    L.append("")
    if overall is not None:
        L.append("- 同条件平均速度比 IDM 高 20% 以上：各场景 SniffDL/IDM 比值的平均 = %+.1f%% → %s" % (
            overall * 100, "✅ 达标" if overall >= 0.2 else "❌ 未达标"))
    else:
        L.append("- 同条件平均速度比 IDM 高 20% 以上：**无法判定**（IDM 未测或无结果）")
    no_lower = []
    for key, ent in result["scenarios"].items():
        v = verdicts[key]
        if v["vs_aria2"] is not None:
            no_lower.append((key, v["vs_aria2"] >= 0))
    if no_lower:
        bad = [k for k, ok in no_lower if not ok]
        L.append("- 不低于 aria2 -x16 -s16：%s" % ("✅ 全部场景不低于" if not bad else "❌ 低于 aria2 的场景：" + "、".join(bad)))
    for key in LEAD_REQUIRED:
        if key in result["scenarios"]:
            v = verdicts[key]
            L.append("- %s 要领先：%s" % (result["scenarios"][key]["desc"],
                                      "✅" if v["pass"] else ("❌" if v["pass"] is False else "无法判定") +
                                      ("（%s）" % "、".join(v["why"]) if v["why"] else "")))
    L.append("")
    L.append("## 明细（每次运行）")
    L.append("")
    L.append("| 场景 | 工具 | 次 | 耗时 s | MB/s | sha256 | 服务器侧（请求/连接/峰值并发/503） | 备注 |")
    L.append("|---|---|---:|---:|---:|:---:|---|---|")
    for key, ent in result["scenarios"].items():
        for t in ("sdl", "aria2", "idm"):
            if t not in ent["tools"]:
                continue
            te = ent["tools"].get(t, {})
            if not te.get("runs"):
                L.append("| %s | %s | — | — | — | — | — | 未测：%s |" % (key, TOOL_NAME[t], te.get("reason", "")))
                continue
            for i, x in enumerate(te["runs"], 1):
                sv = x.get("server") or {}
                svs = "%s/%s/%s/%s" % (sv.get("requests"), sv.get("connections"), sv.get("peak_active"),
                                       sv.get("rejected_503")) if sv else "—"
                note = x.get("error") or x.get("detail") or ""
                L.append("| %s | %s | %d | %s | %s | %s | %s | %s |" % (
                    key, TOOL_NAME[t], i, _fmt(x.get("elapsed"), 3), _fmt(x.get("mbps")) if x.get("ok") else "—",
                    "✅" if x.get("sha_ok") else ("❌" if x.get("sha_ok") is False else "—"), svs,
                    str(note).replace("|", "/").replace("\n", " ")[:160]))
        if ent.get("sha256_consensus"):
            L.append("| %s | 跨工具 sha256 | | | | %s | | 共 %d 种哈希 |" % (
                key, ent["sha256_consensus"][:16] + "…", ent.get("sha256_variants", 1)))
    L.append("")
    L.append("口径：MB = 1048576 字节；平均 = 成功各次速度的算术平均；失败/未测不计入、不补数。"
             "SniffDL 计时为内核内部计时（常驻进程，不含解释器启动），aria2 为进程墙钟，IDM 为 /s 到文件可用。")
    txt = "\n".join(L) + "\n"
    with open(os.path.join(out, "测速报告.md"), "w", encoding="utf-8") as f:
        f.write(txt)
    return txt


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scale", choices=("full", "small"), default="full")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--tools", default="sdl,aria2,idm")
    ap.add_argument("--scenarios", default="A,B,C,D,E,R1,R2")
    ap.add_argument("--parallel", type=int, default=8, help="小文件同时下载数（三家同值）")
    ap.add_argument("--v3", default=os.path.join(REPO, "v3"))
    ap.add_argument("--aria2")
    ap.add_argument("--idm")
    ap.add_argument("--idm-install-note", default=os.environ.get("IDM_INSTALL_NOTE", ""))
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "bench_out"))
    ap.add_argument("--work")
    ap.add_argument("--data")
    ap.add_argument("--timeout", type=int, default=0)
    ap.add_argument("--server-args", default="", help="原样传给 server.py 的额外参数")
    ap.add_argument("--sandbox-note", default="")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--merge", default="", help="逗号分隔的若干结果目录：合并它们的 bench_result.json 到 --out 再出报告")
    a = ap.parse_args(argv)
    a.out = os.path.abspath(a.out)
    if a.merge:
        result = None
        for d in [x for x in a.merge.split(",") if x]:
            r = json.load(open(os.path.join(d, "bench_result.json"), encoding="utf-8"))
            if result is None:
                result = r
                continue
            result["scenarios"].update(r["scenarios"])
            for t, ti in r["tools"].items():
                if result["tools"].get(t, {}).get("status") != "ok":
                    result["tools"][t] = ti
        if a.sandbox_note:
            result["sandbox"] = a.sandbox_note
        os.makedirs(a.out, exist_ok=True)
        json.dump(result, open(os.path.join(a.out, "bench_result.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    elif a.report_only:
        result = json.load(open(os.path.join(a.out, "bench_result.json"), encoding="utf-8"))
    else:
        result = run_all(a)
    txt = write_report(result, a.out)
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
