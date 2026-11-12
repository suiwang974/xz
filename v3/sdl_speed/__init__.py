"""sdl_speed：SniffDL 的 IDM 式动态分片下载引擎。"""
from .engine import DownloadTask, start, download_blocking, file_hash
from .errors import SpeedError, CancelledError
from . import profiles

__all__ = ["DownloadTask", "start", "download_blocking", "file_hash", "SpeedError",
           "CancelledError", "profiles"]
__version__ = "0.2.0"
