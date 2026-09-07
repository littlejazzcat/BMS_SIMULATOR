"""SQLite 报文存储：线程安全长连接 + 批量事务写 + 定期清理 + WAL

写库通过 asyncio.to_thread 在线程池执行，连接加锁保护，check_same_thread=False。
"""

import sqlite3
import threading

DB_PATH = "bms_messages.db"
MAX_ROWS = 50000            # 最多保留 5 万条，超出删最旧
CLEANUP_INTERVAL = 5000     # 每插入 5000 条清理一次

_conn = None
_insert_count = 0
_lock = threading.Lock()


def init_db():
    """初始化数据库 + 建表 + WAL + 降低 fsync 频率"""
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")  # WAL 下安全，大幅提升写性能
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL,
            abbr TEXT,
            can_id INTEGER,
            data_hex TEXT,
            time TEXT NOT NULL
        )"""
    )
    _conn.commit()


def insert_messages(records: list):
    """批量插入一批报文（单个事务，executemany）+ 定期清理"""
    global _insert_count
    if not records or _conn is None:
        return
    with _lock:
        _conn.executemany(
            "INSERT INTO messages (direction, abbr, can_id, data_hex, time) VALUES (?, ?, ?, ?, ?)",
            [(r["direction"], r["abbr"], r["can_id"], r["data_hex"], r["time"]) for r in records],
        )
        _conn.commit()

        _insert_count += len(records)
        if _insert_count >= CLEANUP_INTERVAL:
            _insert_count = 0
            _conn.execute(
                "DELETE FROM messages WHERE id < (SELECT MAX(id) - ? FROM messages)",
                (MAX_ROWS,),
            )
            _conn.commit()
            try:
                _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass


def query_messages(offset: int = 0, limit: int = 200):
    """分页查询（按 id 从新到旧）"""
    if _conn is None:
        return [], 0
    with _lock:
        cur = _conn.cursor()
        total = cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        cur.execute(
            "SELECT direction, abbr, can_id, data_hex, time FROM messages "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = [{"direction": r[0], "abbr": r[1], "can_id": r[2], "data_hex": r[3], "time": r[4]} for r in cur.fetchall()]
    return rows, total


def query_all_messages():
    """查询所有报文（按 id 从旧到新，导出用）"""
    if _conn is None:
        return []
    with _lock:
        cur = _conn.cursor()
        cur.execute("SELECT direction, abbr, can_id, data_hex, time FROM messages ORDER BY id ASC")
        rows = [{"direction": r[0], "abbr": r[1], "can_id": r[2], "data_hex": r[3], "time": r[4]} for r in cur.fetchall()]
    return rows


def clear_messages():
    global _insert_count
    if _conn is None:
        return
    with _lock:
        _conn.execute("DELETE FROM messages")
        _conn.commit()
        try:
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        _insert_count = 0
