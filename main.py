"""BMS 模拟器入口：启动 uvicorn 服务，并自动打开网页"""

import logging
import os
import socket
import threading
import webbrowser

import uvicorn

from paths import app_dir

HOST = "0.0.0.0"
PREFERRED_PORT = 8001   # 首选端口
PORT_SEARCH = 50        # 被占用时向后尝试的端口数量


def find_available_port(preferred: int = PREFERRED_PORT, count: int = PORT_SEARCH) -> int:
    """从 preferred 起查找一个可绑定的端口，返回实际端口号

    注意：Windows 上不使用 SO_REUSEADDR 做探测，否则可能"探测成功"但实际被别人占用。
    只 bind 不 listen 的 socket 关闭后不会进入 TIME_WAIT，不会污染端口。
    """
    for port in range(preferred, preferred + count):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"端口 {preferred}~{preferred + count - 1} 全部被占用，无法启动服务"
    )


def _open_browser(port: int):
    """延迟打开浏览器，等 uvicorn 就绪"""
    webbrowser.open(f"http://localhost:{port}")


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

    log = logging.getLogger("BMS")
    port = find_available_port()
    if port != PREFERRED_PORT:
        log.warning("首选端口 %d 被占用，自动改用 %d", PREFERRED_PORT, port)
    log.info("BMS 模拟器启动，访问 http://localhost:%d", port)

    threading.Timer(1.5, _open_browser, args=(port,)).start()
    uvicorn.run("server.app:app", host=HOST, port=port)
