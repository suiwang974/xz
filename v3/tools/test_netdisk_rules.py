# -*- coding: utf-8 -*-
"""netdisk_rules 单测（纯标准库，零外网）：python tools/test_netdisk_rules.py"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import netdisk_rules as ND  # noqa: E402

QUARK = ("https://dl-pc-sz.drive.quark.cn/ngTX6F13/abc/def?abt=8_0_&auth_key=1793990469-351891-21600-"
         "0d1e2f&sp=100&token=x&filename=hanshuang-pojia-2.0.2.zip")
BAIDU = ("https://d.pcs.baidu.com/file/abcd?fid=1-250528-816614968639868&rt=pr&sign=FDtAER-xx"
         "&expires=8h&chkbd=0&chkv=0&dp-logid=625515150416486205&dp-callid=0&dstime=1793970000"
         "&r=123&time=1793970000&vuk=1&file_type=0")


class Profiles(unittest.TestCase):
    def test_match(self):
        self.assertEqual(ND.match_profile(QUARK)["key"], "quark")
        self.assertEqual(ND.match_profile(BAIDU)["key"], "baidu")
        self.assertEqual(ND.match_profile("https://allall01.baidupcs.com/file/x")["key"], "baidu")
        self.assertEqual(ND.match_profile("https://cn-beijing-data.aliyundrive.net/x?x-oss-expires=1")["key"],
                         "aliyun")
        self.assertEqual(ND.match_profile("https://download.123pan.com/x")["key"], "123pan")
        self.assertEqual(ND.match_profile("https://developer-oss.lanzoug.com/file/x")["key"], "lanzou")
        self.assertEqual(ND.match_profile("https://cdnfhnfile.115cdn.net/x")["key"], "115")
        self.assertIsNone(ND.match_profile("https://github.com/x/y.zip"))
        self.assertIsNone(ND.match_profile("https://notquark.cn.evil.com/x"))     # 后缀匹配不能被骗

    def test_conn(self):
        q = ND.match_profile(QUARK)
        self.assertEqual(ND.profile_conn(q, {}, 16), 16)
        self.assertEqual(ND.profile_conn(q, {"netdisk_conn": {"quark": 4}}, 16), 4)
        self.assertEqual(ND.profile_conn(q, {"netdisk_conn": {"quark": 99}}, 32), 16)   # aria2 硬顶
        self.assertEqual(ND.profile_conn(ND.match_profile("https://a.115cdn.net/x"), {}, 16), 4)
        self.assertEqual(ND.profile_conn(q, {}, 2), 2)    # 全局只开 2 条就听全局


class Headers(unittest.TestCase):
    def test_linkswift_options(self):
        opt = {"user-agent": "UA-OPT", "referer": "https://pan.quark.cn/", "out": "a.zip",
               "header": ["Accept: */*", "User-Agent: UA-HDR", "Origin: https://pan.quark.cn",
                          "Referer: https://pan.quark.cn/", "Cookie: __pus=1; __puus=2", "Dnt: "]}
        h = ND.headers_from_aria2_options(opt)
        self.assertEqual(h["User-Agent"], "UA-HDR")        # header 里给了就以 header 为准
        self.assertEqual(h["Cookie"], "__pus=1; __puus=2")
        self.assertNotIn("Dnt", h)                          # 空值头丢弃
        h2 = ND.headers_from_aria2_options({"user-agent": "netdisk", "header": ["Cookie: BDUSS=1"]})
        self.assertEqual(h2["User-Agent"], "netdisk")       # 只给选项也认

    def test_lowercase_no_dup(self):
        h = ND.parse_header_lines(["user-agent: A", "User-Agent: B", "cookie: c=1"])
        self.assertEqual(len([k for k in h if k.lower() == "user-agent"]), 1)
        self.assertEqual(ND.hget(h, "User-Agent"), "A")

    def test_string_header(self):
        self.assertEqual(ND.parse_header_lines("Authorization: Basic xx")["Authorization"], "Basic xx")

    def test_finalize_never_overrides(self):
        h, prof, added = ND.finalize_headers(BAIDU, {"User-Agent": "LogStatistic", "Range": "bytes=0-"},
                                             app_ua="Chrome")
        self.assertEqual(h["User-Agent"], "LogStatistic")
        self.assertNotIn("Range", h)
        self.assertEqual(added, [])
        h, prof, added = ND.finalize_headers(BAIDU, {}, app_ua="Chrome")
        self.assertEqual(h["User-Agent"], "pan.baidu.com")   # 百度缺 UA 补 pan.baidu.com（OpenList 文档）
        h, prof, added = ND.finalize_headers("https://cn-beijing-data.aliyundrive.net/x", {}, app_ua="C")
        self.assertEqual(h["Referer"], "https://www.alipan.com/")

    def test_custom_headers_box(self):
        t = "User-Agent: netdisk;P2SP;3.0.0.127\nCookie: BDUSS=abc\n"
        h = ND.parse_custom_headers(t)
        self.assertEqual(h["User-Agent"], "netdisk;P2SP;3.0.0.127")
        curl = ("curl -L -C - 'https://d.pcs.baidu.com/file/x' -o 'a.zip' -A 'pan.baidu.com' "
                "-H 'Cookie: BDUSS=abc' --header \"Referer: https://pan.baidu.com/\"")
        h = ND.parse_custom_headers(curl)
        self.assertEqual(h["User-Agent"], "pan.baidu.com")
        self.assertEqual(h["Cookie"], "BDUSS=abc")
        self.assertEqual(h["Referer"], "https://pan.baidu.com/")


class Expiry(unittest.TestCase):
    def test_quark_auth_key(self):
        ts, how = ND.parse_expiry(QUARK)
        self.assertEqual(ts, 1793990469)
        self.assertEqual(how, "auth_key")
        self.assertFalse(ND.expiry_state(QUARK, now=1793990469 - 600)["expired"])
        self.assertTrue(ND.expiry_state(QUARK, now=1793990469 + 600)["expired"])
        self.assertFalse(ND.expiry_state(QUARK, now=1793990469 + 30)["expired"])   # 60s 时钟误差宽限

    def test_other_alicdn_conservative(self):
        ts, how = ND.parse_expiry("https://cdn.example.com/a.zip?auth_key=1793990469-0-0-abc")
        self.assertEqual(ts, 1793990469 + 1800)

    def test_baidu(self):
        ts, how = ND.parse_expiry(BAIDU)
        self.assertEqual(ts, 1793970000 + 8 * 3600)
        self.assertEqual(how, "expires+time")

    def test_oss_s3(self):
        self.assertEqual(ND.parse_expiry("https://x.aliyundrive.net/a?x-oss-expires=1793990000&x-oss-signature=1")[0],
                         1793990000)
        self.assertEqual(ND.parse_expiry("https://b.oss-cn.aliyuncs.com/a?Expires=1793990000&Signature=x")[0],
                         1793990000)
        ts, how = ND.parse_expiry("https://s3.x.com/a?X-Amz-Date=20261106T120000Z&X-Amz-Expires=3600")
        self.assertEqual(how, "x-amz")
        import calendar
        self.assertEqual(ts, calendar.timegm((2026, 11, 6, 12, 0, 0)) + 3600)

    def test_unknown(self):
        self.assertEqual(ND.parse_expiry("https://github.com/a/b/releases/download/v1/x.zip"), (None, ""))
        self.assertIsNone(ND.expiry_state("https://github.com/x.zip")["expired"])
        # 115 的 t= 只在 115 规则下认，别把别人家 t=（多为时间戳/版本号）当到期
        self.assertIsNone(ND.parse_expiry("https://example.com/a?t=1793990000")[0])
        self.assertEqual(ND.parse_expiry("https://cdnfhnfile.115cdn.net/a?t=1793990000")[0], 1793990000)


class Errors(unittest.TestCase):
    def test_status_parse(self):
        self.assertEqual(ND.http_status_from_error("code=22 msg=The response status is not successful. status=412"), 412)
        self.assertEqual(ND.http_status_from_error("HTTP Error 403: Forbidden"), 403)
        self.assertEqual(ND.http_status_from_error("403 Client Error: Forbidden for url"), 403)
        self.assertIsNone(ND.http_status_from_error("connection reset"))

    def test_explain(self):
        now = 1793990469 - 600
        m = ND.explain_failure(QUARK, 412, {}, now=now)
        self.assertIn("缺登录 Cookie", m)
        self.assertIn("412", m)
        m = ND.explain_failure(QUARK, 412, {"Cookie": "__pus=1"}, now=now)
        self.assertIn("Cookie 无效", m)
        m = ND.explain_failure(QUARK, 403, {"Cookie": "__pus=1"}, now=1793990469 + 3600)
        self.assertIn("过期", m)
        m = ND.explain_failure(BAIDU, 403, {"User-Agent": "Mozilla/5.0"}, now=1793970000)
        self.assertIn("UA", m)
        self.assertIn("404", ND.explain_failure("https://x.com/a.zip", 404, {}))
        self.assertIn("限流", ND.explain_failure("https://x.com/a.zip", 429, {}))


class Jar(unittest.TestCase):
    def test_domain_rules(self):
        j = ND.CookieJarLite()
        now = time.time()
        j.update([
            {"name": "__pus", "value": "P", "domain": ".quark.cn", "path": "/", "hostOnly": False,
             "secure": True, "expirationDate": now + 3600},
            {"name": "host_only", "value": "H", "domain": "pan.quark.cn", "path": "/", "hostOnly": True},
            {"name": "old", "value": "O", "domain": ".quark.cn", "path": "/", "expirationDate": now - 10},
            {"name": "BDUSS", "value": "B", "domain": ".baidu.com", "path": "/"},
        ])
        c = j.header_for("https://dl-pc-sz.drive.quark.cn/x?a=1")
        self.assertIn("__pus=P", c)
        self.assertNotIn("host_only", c)      # pan.quark.cn 的 hostOnly Cookie 不发给 CDN 子域
        self.assertNotIn("old", c)            # 过期的不发
        self.assertNotIn("BDUSS", c)
        self.assertEqual(j.header_for("http://dl-pc-sz.drive.quark.cn/x"), "")   # secure 只走 https
        self.assertIn("host_only=H", j.header_for("https://pan.quark.cn/"))


class Aria2Opts(unittest.TestCase):
    def test_opts(self):
        o = ND.aria2_http_task_opts(32, "1M", is_windows=True)
        self.assertEqual(o["max-connection-per-server"], "16")
        self.assertEqual(o["file-allocation"], "falloc")
        self.assertEqual(ND.aria2_http_task_opts(4, "1M")["file-allocation"], "none")
        self.assertIn("--async-dns=false", ND.aria2_http_daemon_args(True))


if __name__ == "__main__":
    r = unittest.main(exit=False, verbosity=1).result
    ok = r.wasSuccessful()
    print("NETDISK RULES UNITTEST RESULT=%s (%d tests)" % ("PASS" if ok else "FAIL", r.testsRun))
    sys.exit(0 if ok else 1)
