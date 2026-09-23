"""并发验收：真实 SQLite 文件上的写争用、连接初始化争用与时间戳一致性。

覆盖（均使用同步屏障让竞争真实发生，而非靠 sleep 碰运气）：

* 多线程同时在全新文件上初始化连接 / 切换 WAL（模拟多应用/多连接首启）；
* 多编辑台同时登记模板并立即生成时间线：每个成功响应对应且只对应一条完整
  记录，锁在超时前释放时等待者必然成功；
* 锁等待超时：稳定可识别的 ``service_unavailable`` 503，不泄漏 SQLite 内部
  错误，不产生半成品行，锁释放后重试成功；
* 每个成功 ID：POST 内容（含 created_at）与重复 GET 逐字一致，模板同理
  （模板无详情接口，直接核对落库行）；created_at 可稳定排序。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from tests.conftest import make_template_payload

CREATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ---- 辅助工具 -------------------------------------------------------------

class _WriteLockHolder:
    """在独立线程里用真实 sqlite3 连接持有写锁，直到收到释放事件。"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn = None
        self.acquired = threading.Event()
        self.release = threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True)

    def start(self):
        self._thread.start()
        if not self.acquired.wait(timeout=5):
            raise RuntimeError("write lock was not acquired")

    def _hold(self):
        # 连接必须在使用它的同一线程内创建。
        conn = sqlite3.connect(self._db_path, timeout=0,
                               isolation_level=None)
        self._conn = conn
        conn.execute("PRAGMA busy_timeout=0")
        # 文件此时已是 WAL；该 PRAGMA 为幂等查询，不与 WAL 切换争用。
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
        conn.execute("BEGIN IMMEDIATE")
        self.acquired.set()
        self.release.wait(timeout=10)
        conn.execute("COMMIT")
        conn.close()

    def unlock(self):
        self.release.set()
        self._thread.join(timeout=5)
        assert not self._thread.is_alive()


def _count(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _persisted_template(db_path: str, template_id: str):
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        row = conn.execute(
            "SELECT body_json, created_at FROM templates WHERE id = ?",
            (template_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return json.loads(row[0]), row[1]


def _created_at_views(db_path: str, table: str):
    """返回 ``(按 id 排序的 (id, created_at) 列表, 按 (created_at, id)
    排序的同批列表)``，用于核对时间戳排序的确定性。"""
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        by_id = [(r[0], r[1]) for r in conn.execute(
            f"SELECT id, created_at FROM {table} ORDER BY id").fetchall()]
        by_time = [(r[0], r[1]) for r in conn.execute(
            f"SELECT id, created_at FROM {table} "
            f"ORDER BY created_at, id").fetchall()]
    finally:
        conn.close()
    return by_id, by_time


@pytest.fixture()
def app_and_client(db_path):
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        yield app, client, db_path


def _post_template(client, index: int):
    payload = make_template_payload()
    # 内容各自不同，便于核对"无重复、可区分"。
    payload["points"].append(
        {"id": f"uniq{index}", "release": index, "latest": 10**9})
    return client.post("/templates", json=payload)


# ---- 连接初始化 / WAL 切换争用 --------------------------------------------

def test_concurrent_init_and_wal_switch_on_fresh_file(tmp_path):
    """多个启动流程同时在全新文件上 init + 切 WAL，全部必须成功。"""
    path = str(tmp_path / "fresh_init.db")
    n = 16
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            # 模拟应用启动：init_db 内部连接会竞争把文件切到 WAL。
            db.init_db(path)
            conn = db._connect(path)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                assert str(mode).lower() == "wal"
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001 - 记录到列表断言
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert errors == []
    # 初始化争用不得放大成失败；后续正常读写一条模板验证文件完好。
    tid, created_at = db.insert_template(
        {"points": [{"id": "a", "release": 0, "latest": None}],
         "relations": []}, path)
    assert CREATED_AT_RE.match(created_at)
    assert db.get_template(tid, path)["points"][0]["id"] == "a"


def test_concurrent_app_starts_share_wal_without_500(tmp_path):
    """多个 create_app（首启路径）同时跑在同一全新文件上，无一抛出。"""
    path = str(tmp_path / "fresh_app.db")
    n = 12
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            create_app(db_path=path)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []

    # 其中一个 app 能立即正常服务。
    with TestClient(create_app(db_path=path)) as client:
        resp = client.post("/templates", json=make_template_payload())
        assert resp.status_code == 201, resp.text


# ---- 并发登记 + 推演：屏障并发 --------------------------------------------

def test_concurrent_registration_and_generation(app_and_client):
    app, client, db_path = app_and_client
    n_workers = 12

    # 共享模板：所有线程同时对它推演，集中竞争 results 写锁。
    shared_tid = client.post(
        "/templates", json=make_template_payload()).json()["template_id"]

    start = threading.Barrier(n_workers + 1)
    done = threading.Barrier(n_workers + 1)
    template_posts: dict[int, dict] = {}
    result_posts: list[dict] = []
    failures: list[str] = []
    data_lock = threading.Lock()

    def worker(index: int):
        start.wait()
        try:
            t_resp = _post_template(client, index)
            if t_resp.status_code != 201:
                with data_lock:
                    failures.append(
                        f"template[{index}] -> {t_resp.status_code} "
                        f"{t_resp.text}")
                return
            t_body = t_resp.json()

            d_resp = client.post(
                "/derivations", json={"template_id": t_body["template_id"]})
            shared_resp = client.post(
                "/derivations", json={"template_id": shared_tid})

            with data_lock:
                template_posts[index] = t_body
                if d_resp.status_code == 201:
                    result_posts.append(d_resp.json())
                else:
                    failures.append(
                        f"own derive[{index}] -> {d_resp.status_code} "
                        f"{d_resp.text}")
                if shared_resp.status_code == 201:
                    result_posts.append(shared_resp.json())
                else:
                    failures.append(
                        f"shared derive[{index}] -> {shared_resp.status_code} "
                        f"{shared_resp.text}")
        finally:
            done.wait()

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(n_workers)]
    for t in threads:
        t.start()
    start.wait()
    done.wait()
    for t in threads:
        t.join(timeout=30)

    assert failures == []

    # ---- 行数核对：每个成功响应恰对应一行，无重复无半成品 ----
    assert _count(db_path, "templates") == n_workers + 1
    assert _count(db_path, "results") == 2 * n_workers

    template_ids = [b["template_id"] for b in template_posts.values()]
    result_ids = [b["result_id"] for b in result_posts]
    assert len(set(template_ids)) == n_workers
    assert len(set(result_ids)) == 2 * n_workers

    # 所有响应的字段集合稳定、时间戳格式正确。
    for body in template_posts.values():
        assert CREATED_AT_RE.match(body["created_at"])
        assert body["point_count"] == len(body["points"])
        assert body["relation_count"] == len(body["relations"])
    for body in result_posts:
        assert CREATED_AT_RE.match(body["created_at"])
        assert set(body) == {"result_id", "template_id", "created_at",
                             "delay", "times", "points"}
        assert [row["id"] for row in body["points"]] == sorted(
            row["id"] for row in body["points"])

    # ---- 每个成功 result：POST 与重复 GET 的完整内容逐字一致 ----
    for post_body in result_posts:
        rid = post_body["result_id"]
        first = client.get(f"/results/{rid}")
        second = client.get(f"/results/{rid}")
        assert first.status_code == second.status_code == 200
        got1, got2 = first.json(), second.json()
        assert got1 == got2 == post_body

    # ---- 每个成功模板：POST 时间戳与落库行逐字一致 ----
    for index, post_body in template_posts.items():
        stored_body, stored_created_at = _persisted_template(
            db_path, post_body["template_id"])
        assert stored_created_at == post_body["created_at"]
        assert stored_body["points"] == post_body["points"]
        assert stored_body["relations"] == post_body["relations"]

    # ---- 排序稳定性：created_at 是可排序的规范时间戳，按它排序对
    #      响应集合与数据库查询结果一致（按时间排序/审计不产生歧义）----
    by_id, by_time = _created_at_views(db_path, "results")
    assert list(by_time) == sorted(by_time, key=lambda x: (x[1], x[0]))
    # 同一 ID 在两种视图中时间戳必须相同。
    assert dict(by_time) == dict(by_id)

    post_pairs = [(b["result_id"], b["created_at"]) for b in result_posts]
    post_by_time = sorted(post_pairs, key=lambda x: (x[1], x[0]))
    # 响应时间戳集合与落库完全相同（同一批记录、逐字一致）。
    assert sorted(post_pairs, key=lambda x: (x[1], x[0])) == by_time
    assert dict(post_pairs) == dict(by_id)


# ---- 锁释放前等待者成功 ----------------------------------------------------

def test_write_succeeds_when_lock_released_within_timeout(app_and_client):
    app, client, db_path = app_and_client
    template_id = client.post(
        "/templates", json=make_template_payload()).json()["template_id"]
    existing = client.post(
        "/derivations", json={"template_id": template_id}).json()

    holder = _WriteLockHolder(db_path)
    holder.start()  # 写锁已被外部连接持有

    # 两个 worker 在屏障处同步起跑；主线程不参与屏障（否则第三方永远凑不齐），
    # 而是等 in_flight 事件确认两者都已进入请求后再检查/释放锁。
    start = threading.Barrier(2)
    in_flight = threading.Event()
    in_flight_lock = threading.Lock()
    in_flight_count = 0
    outcomes: dict[str, object] = {}

    def _mark_in_flight():
        nonlocal in_flight_count
        with in_flight_lock:
            in_flight_count += 1
            if in_flight_count == 2:
                in_flight.set()

    def do_template():
        start.wait()
        _mark_in_flight()
        outcomes["template"] = _post_template(client, 99)

    def do_derivation():
        start.wait()
        _mark_in_flight()
        outcomes["derivation"] = client.post(
            "/derivations", json={"template_id": template_id})

    t1 = threading.Thread(target=do_template)
    t2 = threading.Thread(target=do_derivation)
    t1.start()
    t2.start()
    assert in_flight.wait(timeout=5)

    # 两个请求都在等锁的窗口期内：不得提前写入（证明确实在等锁，而非
    # 报错或重复插入）。
    assert _count(db_path, "templates") == 1
    # WAL 下读者不被写锁阻塞：查询既有结果始终成功。
    got = client.get(f"/results/{existing['result_id']}")
    assert got.status_code == 200 and got.json() == existing

    holder.unlock()  # 0.4s 量级的持有，远小于默认 5s busy_timeout
    t1.join(timeout=10)
    t2.join(timeout=10)

    t_resp = outcomes["template"]
    d_resp = outcomes["derivation"]
    assert t_resp.status_code == 201, t_resp.text
    assert d_resp.status_code == 201, d_resp.text
    assert _count(db_path, "templates") == 2
    assert _count(db_path, "results") == 2

    # 等到锁后成功的记录同样满足 POST == GET 逐字一致。
    assert client.get(
        f"/results/{d_resp.json()['result_id']}").json() == d_resp.json()
    _, stored_at = _persisted_template(db_path, t_resp.json()["template_id"])
    assert stored_at == t_resp.json()["created_at"]


# ---- 等待超时：稳定 503，无半成品，重试成功 --------------------------------

def test_write_timeout_returns_stable_503_and_no_half_row(
        app_and_client, monkeypatch):
    app, client, db_path = app_and_client
    monkeypatch.setenv("BROADCAST_DB_BUSY_TIMEOUT", "0.5")

    template_id = client.post(
        "/templates", json=make_template_payload()).json()["template_id"]
    before_templates = _count(db_path, "templates")
    before_results = _count(db_path, "results")

    holder = _WriteLockHolder(db_path)
    holder.start()

    start = threading.Barrier(2)
    in_flight = threading.Event()
    in_flight_lock = threading.Lock()
    in_flight_count = 0
    responses: dict[str, object] = {}

    def _mark_in_flight():
        nonlocal in_flight_count
        with in_flight_lock:
            in_flight_count += 1
            if in_flight_count == 2:
                in_flight.set()

    def do_template():
        start.wait()
        _mark_in_flight()
        responses["template"] = _post_template(client, 7)

    def do_derivation():
        start.wait()
        _mark_in_flight()
        responses["derivation"] = client.post(
            "/derivations", json={"template_id": template_id})

    threads = [threading.Thread(target=do_template),
               threading.Thread(target=do_derivation)]
    for t in threads:
        t.start()
    assert in_flight.wait(timeout=5)
    for t in threads:
        t.join(timeout=15)

    for key in ("template", "derivation"):
        resp = responses[key]
        assert resp.status_code == 503, resp.text
        err = resp.json()["error"]
        assert err["code"] == "service_unavailable"
        assert isinstance(err["message"], str) and err["message"]
        lowered = err["message"].lower()
        assert "sqlite" not in lowered and "locked" not in lowered
        assert err["details"] == {"retry_after_seconds": 1}

    # 超时失败：零新行，绝无半成品。
    assert _count(db_path, "templates") == before_templates
    assert _count(db_path, "results") == before_results

    # 锁释放后按原请求体重试：成功且只产生一行，POST == GET。
    holder.unlock()
    ok_template = _post_template(client, 7)
    ok_derivation = client.post(
        "/derivations", json={"template_id": template_id})
    assert ok_template.status_code == 201
    assert ok_derivation.status_code == 201
    assert _count(db_path, "templates") == before_templates + 1
    assert _count(db_path, "results") == before_results + 1
    assert client.get(
        f"/results/{ok_derivation.json()['result_id']}").json() \
        == ok_derivation.json()
    _, stored_at = _persisted_template(
        db_path, ok_template.json()["template_id"])
    assert stored_at == ok_template.json()["created_at"]


def test_template_post_created_at_matches_persisted_row(app_and_client):
    """单写流程下模板创建时间同样不分裂。"""
    app, client, db_path = app_and_client
    resp = client.post("/templates", json=make_template_payload())
    assert resp.status_code == 201
    body = resp.json()
    _, stored_at = _persisted_template(db_path, body["template_id"])
    assert stored_at == body["created_at"]
    assert CREATED_AT_RE.match(stored_at)
