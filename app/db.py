"""SQLite 持久层：合法模板与成功推演结果。

数据库文件位于挂载卷内（默认 ``/data/app.db``，可用环境变量
``BROADCAST_DB_PATH`` 覆盖）。每次操作使用短连接，开启 WAL 与外键约束。

只有两种写操作：

* 登记一个**已经通过校验**的模板；
* 保存一次**已经成功**的推演结果。

非法模板与失败推演根本不会进入本模块，因此失败请求既不会产生记录，
也不会改动既有数据。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid

DEFAULT_DB_PATH = "/data/app.db"
LOCAL_FALLBACK_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "local-data", "app.db")

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


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or resolve_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: str | None = None) -> None:
    """创建表结构（幂等）。"""
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def new_id() -> str:
    return uuid.uuid4().hex


def insert_template(template: dict, db_path: str | None = None) -> str:
    """插入已校验的模板，返回生成的模板 ID。"""
    template_id = new_id()
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO templates (id, body_json) VALUES (?, ?)",
            (template_id, json.dumps(template, ensure_ascii=False,
                                     sort_keys=True)),
        )
        conn.commit()
    finally:
        conn.close()
    return template_id


def get_template(template_id: str, db_path: str | None = None):
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT body_json FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return json.loads(row["body_json"])


def insert_result(template_id: str, delay: dict, times: dict,
                  db_path: str | None = None) -> str:
    """插入一次成功推演的结果，返回生成的结果 ID。"""
    result_id = new_id()
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO results (id, template_id, delay_json, times_json) "
            "VALUES (?, ?, ?, ?)",
            (result_id, template_id,
             json.dumps(delay, ensure_ascii=False, sort_keys=True),
             json.dumps(times, ensure_ascii=False, sort_keys=True)),
        )
        conn.commit()
    finally:
        conn.close()
    return result_id


def get_result(result_id: str, db_path: str | None = None):
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT template_id, delay_json, times_json, created_at "
            "FROM results WHERE id = ?",
            (result_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "id": result_id,
        "template_id": row["template_id"],
        "delay": json.loads(row["delay_json"]),
        "times": json.loads(row["times_json"]),
        "created_at": row["created_at"],
    }
