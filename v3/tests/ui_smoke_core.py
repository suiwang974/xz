# -*- coding: utf-8 -*-
"""冒烟用内核启动器：无图形环境下给 tkinter 打桩，再跑真 sniffdl_core.main（参数原样透传）。"""
import os
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stub_tk():
    try:
        import tkinter  # noqa: F401
        return
    except Exception:
        pass

    class _Any:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, n):
            return _Any()

        def __call__(self, *a, **k):
            return _Any()

    def _mod_getattr(n):
        if n.startswith("__"):
            raise AttributeError(n)
        return _Any

    for m in ("tkinter", "tkinter.font", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox"):
        mod = types.ModuleType(m)
        mod.__getattr__ = _mod_getattr     # 双下划线名必须 AttributeError，否则 inspect/Playwright 会把 _Any 当 __file__ 炸掉
        sys.modules[m] = mod
    sys.modules["tkinter"].font = sys.modules["tkinter.font"]


if __name__ == "__main__":
    _stub_tk()
    sys.path.insert(0, HERE)
    import sniffdl_core
    sys.exit(sniffdl_core.main(sys.argv[1:]))
