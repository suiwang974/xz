"""sdl_speed 的错误类型：每个错误都带 kind（机器可读）+ 中文原因（直接给用户看）。

kind 取值：
  gone       404 / 410：文件不存在或已删除（调用方应直接失败，不要回退）
  expired    链接签名过期（403 + 链接里的过期时间已过，由调用方判断或 profiles 提示）
  forbidden  401 / 403：被拒绝（多半是 Cookie / Referer / UA 不对）
  throttled  429 / 503 重试耗尽
  http       其他 HTTP 状态码
  network    网络层错误（连接失败、重置、超时）重试耗尽
  changed    服务器上的文件已变化（大小 / ETag / Last-Modified 不一致）
  verify     下载完成但大小或哈希校验失败
  io         本地磁盘错误（空间不足、无权限……）
  cancelled  用户取消
"""

FATAL_LINK_KINDS = ("gone", "expired")


class SpeedError(Exception):
    def __init__(self, kind, message, status=None, detail=""):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.message = message      # 中文原因
        self.detail = detail        # 英文原始信息，便于日志

    def __str__(self):
        s = self.message
        if self.status:
            s = "%s（HTTP %s）" % (s, self.status)
        return s

    @property
    def fatal_link(self):
        """链接本身已不可用（404/410/过期）：回退到别的下载器也是白费。"""
        return self.kind in FATAL_LINK_KINDS


class CancelledError(SpeedError):
    def __init__(self, message="已取消"):
        super().__init__("cancelled", message)


_STATUS_CN = {
    400: "请求格式被服务器拒绝",
    401: "需要登录或授权（Cookie / Token 无效）",
    403: "服务器拒绝访问（多半是 Cookie、Referer 或 UA 不对，或链接已过期）",
    404: "文件不存在（链接失效或已被删除）",
    405: "服务器不允许这种请求方式",
    410: "文件已被删除",
    412: "服务器风控拦截（请求头和浏览器不一致）",
    416: "请求的范围无效（服务器上的文件可能已变化）",
    429: "请求过于频繁，被服务器限流",
    500: "服务器内部错误",
    502: "网关错误（服务器或代理暂时故障）",
    503: "服务器繁忙或限流",
    504: "网关超时",
}


def http_error(status, detail=""):
    status = int(status)
    if status in (404, 410):
        kind = "gone"
    elif status in (401, 403):
        kind = "forbidden"
    elif status in (429, 503):
        kind = "throttled"
    elif status == 416:
        kind = "changed"
    else:
        kind = "http"
    msg = _STATUS_CN.get(status) or ("服务器返回错误 %d" % status)
    return SpeedError(kind, msg, status=status, detail=detail)


def network_error(exc):
    name = type(exc).__name__
    text = str(exc)
    low = (name + " " + text).lower()
    if "timed out" in low or "timeout" in low:
        msg = "网络超时（服务器长时间没有响应）"
    elif "reset" in low or "broken pipe" in low or "aborted" in low or "remotedisconnected" in low:
        msg = "连接被服务器重置"
    elif "name or service" in low or "nodename" in low or "getaddrinfo" in low or "resolve" in low:
        msg = "域名解析失败（检查网络或代理）"
    elif "refused" in low:
        msg = "连接被拒绝（服务器没开或代理不可用）"
    elif "ssl" in low or "certificate" in low:
        msg = "TLS/证书错误"
    elif "proxy" in low:
        msg = "代理连接失败"
    else:
        msg = "网络错误"
    return SpeedError("network", msg, detail="%s: %s" % (name, text[:300]))


def io_error(exc):
    import errno
    code = getattr(exc, "errno", None)
    if code == errno.ENOSPC:
        msg = "磁盘空间不足"
    elif code in (errno.EACCES, errno.EPERM):
        msg = "没有写入权限"
    else:
        msg = "写入文件失败"
    return SpeedError("io", msg, detail=str(exc)[:300])
