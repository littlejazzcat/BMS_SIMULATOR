"""BMS 模拟器入口：启动 uvicorn 服务，并自动打开网页"""

import logging
import os
import threading
import webbrowser

import uvicorn

from paths import app_dir

HOST = "0.0.0.0"
PORT = 8000


def _open_browser():
    """延迟打开浏览器，等 uvicorn 就绪"""
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    # 切到 app 目录：zlgcan.py 用 "./zlgcan.dll" 相对路径加载，保证工作目录正确
    try:
        os.chdir(app_dir())
    except Exception:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("BMS").info("BMS 模拟器启动，访问 http://localhost:%d", PORT)
    threading.Timer(1.5, _open_browser).start()
    uvicorn.run("server.app:app", host=HOST, port=PORT)
