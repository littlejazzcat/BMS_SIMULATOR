"""资源路径解析：兼容「源码运行」和「PyInstaller 打包成 exe」

- 打包后（sys.frozen）：内置资源从 sys._MEIPASS（onedir 的 _internal 目录）取
- 开发运行：资源从项目根目录取
- 运行时可写数据（数据库等）：放在 exe 所在目录（或项目根）
"""

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的 exe 里"""
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """exe 所在目录（打包）或项目根目录（开发）"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(rel: str) -> str:
    """只读资源路径（协议 JSON、web 静态文件等），打包后在 _MEIPASS 下"""
    if is_frozen():
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent
    return str(base / rel)


def data_path(name: str) -> str:
    """运行时可写数据路径（数据库等），放在 exe 旁边，便于用户查看/清理"""
    return str(app_dir() / name)
