"""SQLite 持久层：合法模板与成功推演结果。

数据库文件位于挂载卷内（默认 ``/data/app.db``，可用环境变量
``BROADCAST_DB_PATH`` 覆盖）。每次操作使用短连接，开启 WAL 与外键约束。
写锁等待上限可用 ``BROADCAST_DB_BUSY_TIMEOUT``（秒，默认 5）配置。

只有两种写操作：

* 登记一个**已经通过校验**的模板；
* 保存一次**已经成功**的推演结果。

非法模板与失败推演根本不会进入本模块，因此失败请求既不会产生记录，
也不会改动既有数据。

并发语义（短暂写争用必须在有界时间内得到确定结果）：

* 每个连接先安装 ``busy_timeout``，再以有界重试把数据库切到 WAL；
  一旦某连接完成切换，后续连接看到的已是 WAL（PRAGMA 只查询不写入）。
* 所有写事务都以 ``BEGIN IMMEDIATE`` 开始——**先取得写锁**再写数据。
  锁被占用时在 busy_timeout 内等待；锁提前释放即写入成功。
* 超时只可能发生在取得写锁之前（或提交未成功时），此时一律回滚并抛
  :class:`~app.errors.AppError`（``service_unavailable``），**绝不会**
  留下半成品行：每个 201 响应对应且只对应一条完整记录，每个 503 对应
  零条记录，失败后重试不会产生无法区分的重复。
* ``created_at`` 由应用生成一次后显式写入，响应中返回的正是落入行内的
  同一字符串，创建响应 / 列表 / 详情逐字一致（不依赖 SQLite 的 DEFAULT，
  但保留列 DEFAULT 以兼容既有数据库文件）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from .errors import AppError, SERVICE_UNAVAILABLE

DEFAULT_DB_PATH = "/data/app.db"
LOCAL_FALLBACK_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "local-data", "app.db")

# 写锁争用的有界等待时间（秒）；可用环境变量覆盖。
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
MAX_BUSY_TIMEOUT_SECONDS = 30.0
_WAL_POLL_INTERVAL_SECONDS = 0.02

# SQLite 主错误码（旧版 Python 上 sqlite3 模块未必导出常量，直接用字面值）。
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id          TEXT PRIMARY KEY,
    body_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS results (
    id           TEXT PRIMARY KEY,
    template_id  TEXT NOT NULL,
    delay_json   TEXT NOT NULL,
    times_json   TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (template_id) REFERENCES templates(id)
);

CREATE INDEX IF NOT EXISTS idx_results_template_id
    ON results(template_id);
"""


def utc_now_ms() -> str:
    """UTC 毫秒时间戳，形如 ``2026-09-22T10:00:00.000Z``。

    与 SQLite ``strftime('%Y-%m-%dT%H:%M:%fZ','now')`` 的格式逐字一致，
    因此同一值无论出现在响应还是既有行中都可直接比较、排序。
    """
    now = datetime.now(timezone.utc)
    milliseconds = now.microsecond // 1000
    return now.strftime("%Y-%m-%dT%H:%M:%S") + f".{milliseconds:03d}Z"


def resolve_db_path() -> str:
    configured = os.environ.get("BROADCAST_DB_PATH")
    if configured:
        return configured
    # 容器内 /data 由 Compose 挂载且可写；本地直接运行（无挂载、无权限）时
    # 回退到仓库内的 local-data 目录（已被 .gitignore 排除）。
    data_dir = os.path.dirname(DEFAULT_DB_PATH)
    if os.path.isdir(data_dir) and os.access(data_dir, os.W_OK):
        return DEFAULT_DB_PATH
    return LOCAL_FALLBACK_DB_PATH


def _busy_timeout_seconds() -> float:
    raw = os.environ.get("BROADCAST_DB_BUSY_TIMEOUT")
    if raw is None:
        return DEFAULT_BUSY_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_BUSY_TIMEOUT_SECONDS
    return min(max(value, 0.0), MAX_BUSY_TIMEOUT_SECONDS)


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    """识别 SQLITE_BUSY / SQLITE_LOCKED，不依赖错误消息的语言/版本。"""
    code = getattr(exc, "sqlite_errorcode", None)
    if code in (_SQLITE_BUSY, _SQLITE_LOCKED):
        return True
    message = str(exc).lower()
    return ("database is locked" in message
            or "database table is locked" in message)


def _write_unavailable() -> AppError:
    """写争用在有界等待后仍未解除时的稳定响应（不暴露 SQLite 内部信息）。"""
    return AppError(
        SERVICE_UNAVAILABLE,
        "The storage is temporarily busy with another write; retry the "
        "request after a short wait. No record was created.",
        status_code=503,
        details={"retry_after_seconds": 1})


def _ensure_wal_mode(conn: sqlite3.Connection, timeout: float) -> None:
    """把数据库切到 WAL，容忍多连接同时首启时的短暂争用。

    切换需要排他锁；多个连接在全新文件上同时执行时，SQLite 可能立刻返回
    当前（旧）模式而不是阻塞。因此在有界时间内轮询重试，并以**实际返回
    的模式**为准；已是 WAL 时完全不触发写入。
    """
    current = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(current).lower() == "wal":
        return

    deadline = time.monotonic() + max(timeout, 1.0)
    while True:
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        except sqlite3.OperationalError as exc:
            if _is_lock_error(exc) and time.monotonic() < deadline:
                time.sleep(_WAL_POLL_INTERVAL_SECONDS)
                continue
            raise _write_unavailable()
        if str(mode).lower() == "wal":
            return
        if time.monotonic() >= deadline:
            raise _write_unavailable()
        time.sleep(_WAL_POLL_INTERVAL_SECONDS)


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or resolve_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    timeout = _busy_timeout_seconds()
    # isolation_level=None：显式管理事务（写路径使用 BEGIN IMMEDIATE），
    # 避免 Python sqlite3 在 DML 前隐式开事务造成锁获取时机不明。
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # busy_timeout 必须先于任何可能取锁的语句安装。
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_wal_mode(conn, timeout)
    return conn


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def init_db(db_path: str | None = None) -> None:
    """创建表结构（幂等）。"""
    conn = _connect(db_path)
    try:
        try:
            conn.executescript(_SCHEMA)
        except sqlite3.OperationalError as exc:
            if _is_lock_error(exc):
                raise _write_unavailable()
            raise
    finally:
        conn.close()


def new_id() -> str:
    return uuid.uuid4().hex


def _write_row(db_path: str | None, sql: str, params: tuple) -> None:
    """在单个 ``BEGIN IMMEDIATE`` 事务中写入一行。

    先拿写锁再写数据：锁竞争在 busy_timeout 内有界等待；超时则事务从未
    开始（或提交未成功并已回滚），数据库中不会出现半成品行。
    """
    conn = _connect(db_path)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_lock_error(exc):
                raise _write_unavailable()
            raise
        try:
            conn.execute(sql, params)
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            _safe_rollback(conn)
            if _is_lock_error(exc):
                raise _write_unavailable()
            raise
        except BaseException:
            _safe_rollback(conn)
            raise
    finally:
        conn.close()


def insert_template(template: dict,
                    db_path: str | None = None) -> tuple[str, str]:
    """插入已校验的模板，返回 ``(模板 ID, 落库 created_at)``。

    ``created_at`` 在此生成一次并显式写入，返回值即行内值。
    """
    template_id = new_id()
    created_at = utc_now_ms()
    _write_row(
        db_path,
        "INSERT INTO templates (id, body_json, created_at) "
        "VALUES (?, ?, ?)",
        (template_id,
         json.dumps(template, ensure_ascii=False, sort_keys=True),
         created_at),
    )
    return template_id, created_at


def get_template(template_id: str, db_path: str | None = None):
    conn = _connect(db_path)
    try:
        try:
            row = conn.execute(
                "SELECT body_json FROM templates WHERE id = ?", (template_id,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if _is_lock_error(exc):
                raise _write_unavailable()
            raise
    finally:
        conn.close()
    if row is None:
        return None
    return json.loads(row["body_json"])


def insert_result(template_id: str, delay: dict, times: dict,
                  db_path: str | None = None) -> tuple[str, str]:
    """插入一次成功推演的结果，返回 ``(结果 ID, 落库 created_at)``。

    ``created_at`` 在此生成一次并显式写入，返回值即行内值。
    """
    result_id = new_id()
    created_at = utc_now_ms()
    _write_row(
        db_path,
        "INSERT INTO results (id, template_id, delay_json, times_json, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (result_id, template_id,
         json.dumps(delay, ensure_ascii=False, sort_keys=True),
         json.dumps(times, ensure_ascii=False, sort_keys=True),
         created_at),
    )
    return result_id, created_at


def get_result(result_id: str, db_path: str | None = None):
    conn = _connect(db_path)
    try:
        try:
            row = conn.execute(
                "SELECT template_id, delay_json, times_json, created_at "
                "FROM results WHERE id = ?",
                (result_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if _is_lock_error(exc):
                raise _write_unavailable()
            raise
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "id": result_id,
        "template_id": row["template_id"],
        "delay": json.loads(row["delay_json"]),
        "times": json.loads(row["times_json"]),
        # 详情接口必须逐字返回创建时写入的时间戳。
        "created_at": row["created_at"],
    }
