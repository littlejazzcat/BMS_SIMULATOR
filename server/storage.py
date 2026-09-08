"""SQLite 报文存储：线程安全长连接 + 批量事务写 + 定期清理 + WAL

写库通过 asyncio.to_thread 在线程池执行，连接加锁保护，check_same_thread=False。
"""

import json
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
            time TEXT NOT NULL,
            parsed TEXT
        )"""
    )
    # 迁移：旧表无 parsed 字段 → 重建（历史数据无用，直接丢弃）
    cols = [r[1] for r in _conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "parsed" not in cols:
        _conn.execute("DROP TABLE messages")
        _conn.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,
                abbr TEXT,
                can_id INTEGER,
                data_hex TEXT,
                time TEXT NOT NULL,
                parsed TEXT
            )"""
        )
    _conn.commit()
    # 每次启动清空历史报文
    _conn.execute("DELETE FROM messages")
    _conn.commit()


def insert_messages(records: list):
    """批量插入一批报文（单个事务，executemany）+ 定期清理"""
    global _insert_count
    if not records or _conn is None:
        return
    with _lock:
        _conn.executemany(
            "INSERT INTO messages (direction, abbr, can_id, data_hex, time, parsed) VALUES (?, ?, ?, ?, ?, ?)",
            [(r["direction"], r["abbr"], r["can_id"], r["data_hex"], r["time"],
              json.dumps(r["parsed"], ensure_ascii=False) if r.get("parsed") else None) for r in records],
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


def query_messages(offset: int = 0, limit: int = 200, max_id: int = None):
    """分页查询（按 id 从新到旧）。

    max_id 为分页快照锚点：给定则只查 id <= max_id 的报文，冻结分页窗口，
    不受后续新报文影响。返回值 anchor 为本次快照锚点（max_id 缺省时为当前最大 id）。
    """
    if _conn is None:
        return [], 0, None
    with _lock:
        cur = _conn.cursor()
        if max_id is None:
            total = cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            anchor = cur.execute("SELECT MAX(id) FROM messages").fetchone()[0]
            cur.execute(
                "SELECT direction, abbr, can_id, data_hex, time, parsed FROM messages "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        else:
            total = cur.execute(
                "SELECT COUNT(*) FROM messages WHERE id <= ?", (max_id,)
            ).fetchone()[0]
            anchor = max_id
            cur.execute(
                "SELECT direction, abbr, can_id, data_hex, time, parsed FROM messages "
                "WHERE id <= ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (max_id, limit, offset),
            )
        rows = [{"direction": r[0], "abbr": r[1], "can_id": r[2], "data_hex": r[3], "time": r[4],
                 "parsed": json.loads(r[5]) if r[5] else None} for r in cur.fetchall()]
    return rows, total, anchor


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
