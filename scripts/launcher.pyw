# -*- coding: utf-8 -*-
"""Librarium 静默智能启动器（双击无窗口启动）。

先探测本地后端 http://127.0.0.1:8000/api/health；已运行则直接打开浏览器；
未运行则用 CREATE_NO_WINDOW 静默启动 scripts/server.py（日志写入 workspace/server.log），
每 0.25s 轮询就绪后打开浏览器；超时弹窗引导查看日志。
"""

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "scripts" / "server.py"
LOG_DIR = ROOT / "workspace"
LOG_FILE = LOG_DIR / "server.log"

HEALTH_URL = "http://127.0.0.1:8000/api/health"
HOME_URL = "http://localhost:8000"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def is_running():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=0.6) as resp:
            return resp.status == 200
    except Exception:
        return False


def open_browser(url):
    try:
        if os.name == "nt":
            os.startfile(url)
        else:
            import webbrowser
            webbrowser.open(url)
    except Exception:
        import webbrowser
        webbrowser.open(url)


def start_server():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("a", encoding="utf-8", errors="replace")
    kwargs = dict(cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if CREATE_NO_WINDOW:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.Popen([sys.executable, str(SERVER)], **kwargs)


def show_error():
    msg = "Librarium 本地服务启动失败，请查看 workspace/server.log 排查。"
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "Librarium", 0x00000010)
            return
    except Exception:
        pass
    print(msg)


def main():
    if is_running():
        open_browser(HOME_URL)
        return

    try:
        proc = start_server()
    except Exception:
        proc = None

    deadline = time.time() + 8.0
    while time.time() < deadline:
        if is_running():
            open_browser(HOME_URL)
            return
        if proc is not None and proc.poll() is not None:
            break  # 后端已退出（启动失败）
        time.sleep(0.25)

    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    show_error()


if __name__ == "__main__":
    main()
