# 直播提示点播控服务（broadcast-cue-api）

纯后端服务：登记直播彩排的**提示点模板**，在某些提示点被要求推迟后，
推演同时满足全部下界的**逐点最早可执行时刻**，并把成功结果持久化到挂载卷中的
SQLite。推演对规则的输入顺序不敏感——同一批规则无论怎样排列，只会得到
**同一份**最早提示表；不可实现时给出明确的稳定错误代码，绝不伪装成排期。

- 框架：FastAPI（Python 3.11）
- 存储：SQLite（WAL），默认数据库文件 `/data/app.db`（位于 Docker 挂载卷）
- 编排：Docker Compose，HTTP 入口仅绑定本机 `http://127.0.0.1:8000`
- 交互文档：服务启动后访问 `http://127.0.0.1:8000/docs`

## 目录结构

```
app/
  main.py        # FastAPI 入口：/templates、/derivations、/results/{id}
  scheduler.py   # 最早时刻推演（最长路 / Bellman-Ford 式松弛，不枚举候选时间）
  validation.py  # 模板与 delay 覆盖的严格校验 + 规范化
  db.py          # SQLite 持久层（只写入合法模板与成功结果）
  errors.py      # 稳定错误码与统一错误响应
tests/           # pytest：分支汇合/乱序/延误传播/零间隔环/正权环 + 接口与持久化
Dockerfile
docker-compose.yml
```

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build -d
curl http://127.0.0.1:8000/health      # {"status":"ok"}
docker compose logs -f api
docker compose down
```

SQLite 数据库、WAL、SHM 文件都保存在宿主目录 `./data/`（容器内 `/data`），
容器重启或重建后模板与成功结果仍然可取回。

### 本地直接运行（无 Docker 时）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 无 /data 写权限时自动回退到 ./local-data/app.db；也可用 BROADCAST_DB_PATH 指定
```

### 运行测试

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
```

## 数据模型与约束（普通 JSON）

### 模板

- `points`：1～300 个提示点。
  - `id`：非空字符串，模板内**唯一**。
  - `release`：整数，0 ≤ release ≤ 10⁹。
  - `latest`：可选；给出时必须是整数且 **latest ≥ release**。
- `relations`：0～3000 条先后关系。
  - `from`、`to`：必须引用模板中存在的点 ID（允许自环）。
  - `min_gap`：整数，0 ≤ min_gap ≤ 10⁶，含义是
    **to 的时刻 ≥ from 的时刻 + min_gap**。

### delay 覆盖

推演时可以为**部分**提示点提供 `delay`，值必须是非负整数且
**不小于该点原 release**；语义是把该点的基础下界从 release 替换为 delay。

## 接口约定

所有请求/响应均为 JSON。失败响应统一为：

```json
{ "error": { "code": "<稳定代码>", "message": "<说明>", "details": { } } }
```

| 方法 & 路径 | 说明 | 成功状态 |
|---|---|---|
| `GET /health` | 健康检查 | 200 |
| `POST /templates` | 校验并登记合法模板（返回模板 ID 与规范化内容） | 201 |
| `POST /derivations` | 对已登记模板执行延误推演，成功才落库 | 201 |
| `GET /results/{result_id}` | 按结果 ID 取回成功结果（重启后仍可查） | 200 |

### 稳定错误代码

| code | HTTP | 触发条件 |
|---|---|---|
| `invalid_template` | 400 | 模板违反任一登记规则；**非法模板不落库** |
| `bad_json` | 400 | 请求体不是合法 JSON 或不是 JSON 对象 |
| `invalid_delay` | 400 | delay 引用不存在的点、为负、或小于原 release |
| `template_not_found` | 404 | 推演引用了不存在的模板 ID |
| `result_not_found` | 404 | 查询的结果 ID 不存在（含失败推演——它们从不生成记录） |
| `positive_cycle` | 422 | 规则存在总 min_gap 为正的有向环，不存在有限最早时刻 |
| `deadline_exceeded` | 422 | 无正权环，但至少一点最早时刻 > latest |
| `not_found` / `method_not_allowed` | 404 / 405 | 未知路径 / 方法不允许 |

判定优先级：**先 positive_cycle，再 deadline_exceeded**。
`positive_cycle` / `deadline_exceeded` 都是推演失败：不产生结果记录，
也不改动任何既有数据。

成功结果中的 `times` 与 `points` 一律按提示点 ID **升序**排列。

## 调用示例

### 1. 登记模板

```bash
curl -s http://127.0.0.1:8000/templates \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"id": "a", "release": 0, "latest": 100},
      {"id": "b", "release": 0, "latest": 100},
      {"id": "c", "release": 0, "latest": 100},
      {"id": "d", "release": 1, "latest": 100}
    ],
    "relations": [
      {"from": "a", "to": "b", "min_gap": 5},
      {"from": "a", "to": "c", "min_gap": 20},
      {"from": "b", "to": "d", "min_gap": 3},
      {"from": "c", "to": "d", "min_gap": 0}
    ]
  }'
```

响应（`points`/`relations` 已规范化排序，省略）：

```json
{
  "template_id": "9f0c2b7e...",
  "created_at": "2026-09-22T10:00:00.000Z",
  "point_count": 4,
  "relation_count": 4,
  "points": [ "...按 id 升序..." ],
  "relations": [ "...按 (from,to,min_gap) 升序..." ]
}
```

### 2. 推演：把提示点 a 推迟到 30

```bash
TID=<上一步返回的 template_id>
curl -s http://127.0.0.1:8000/derivations \
  -H 'Content-Type: application/json' \
  -d "{\"template_id\":\"$TID\",\"delay\":{\"a\":30}}"
```

```json
{
  "result_id": "1b2d4f...",
  "template_id": "9f0c2b7e...",
  "created_at": "2026-09-22T10:01:00.000Z",
  "delay": {"a": 30},
  "times": {"a": 30, "b": 35, "c": 50, "d": 50},
  "points": [
    {"id": "a", "release": 0, "latest": 100, "time": 30},
    {"id": "b", "release": 0, "latest": 100, "time": 35},
    {"id": "c", "release": 0, "latest": 100, "time": 50},
    {"id": "d", "release": 1, "latest": 100, "time": 50}
  ]
}
```

无 delay 的推演：`{"template_id":"<id>"}` 或 `{"template_id":"<id>","delay":{}}`。

### 3. 取回结果（容器重启后仍有效）

```bash
curl -s http://127.0.0.1:8000/results/<result_id>
```

### 4. 不可实现：正权环

```bash
curl -s -i http://127.0.0.1:8000/derivations \
  -H 'Content-Type: application/json' \
  -d '{"template_id":"<带 a->b(1), b->a(1) 的模板 ID>"}'
# HTTP/1.1 422
# {"error":{"code":"positive_cycle",
#   "message":"The rules contain a directed cycle with positive total
#   min_gap; no finite earliest schedule exists."}}
```

### 5. 不可实现：超过 latest

```json
HTTP 422
{
  "error": {
    "code": "deadline_exceeded",
    "message": "At least one point's earliest time exceeds its latest bound.",
    "details": {"violations": [{"id": "b", "earliest": 8, "latest": 5}]}
  }
}
```

### 6. 非法模板（不落库）

```json
HTTP 400
{ "error": { "code": "invalid_template",
             "message": "relations[0].to references unknown point id 'ghost'.",
             "details": {"path": "relations[0].to", "id": "ghost"} } }
```

## 算法说明（为什么结果唯一且不需要枚举候选时间）

每条约束都是一个**下界**：

- `t[p] ≥ release[p]`（有 delay 时为 `t[p] ≥ delay[p]`）；
- `t[v] ≥ t[u] + min_gap(u,v)`。

逐点最早时刻等价于一张含"虚拟源点"的图上的**最长路**：源点向每个点连权为
基础下界的边。求法是 Bellman-Ford 式的逐轮**松弛**——每轮扫描全部边，
`t[v] = max(t[v], t[u]+gap)`，直到一轮内无变化。松弛只做最大值传播，
**不枚举任何候选时间**：

- 复杂度 O(n·m)（n ≤ 300，m ≤ 3000）；
- 最终不动点由约束集合唯一确定，与点和边的扫描顺序无关（顺序只影响收敛轮数），
  所以分支重新汇合时更晚的前驱不会漏算，同一批规则永远得到同一张表；
- n 轮完整松弛后若仍有边可松弛，说明存在总权为正的有向环
  （时刻可沿环无限增长）⇒ `positive_cycle`；
- 否则对每个点核对 `latest`，超限即 `deadline_exceeded`；
- 总权为 0 的环（含 0 权自环）合法：它只会把环上各点拉平到同一最早时刻。
