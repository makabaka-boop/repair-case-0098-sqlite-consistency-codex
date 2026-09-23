"""最早可执行时刻推演（差分约束的最长路模型）。

每个提示点 ``p`` 满足：

* ``t[p] >= release[p]``（若提供 delay 覆盖，则 ``t[p] >= delay[p]``）；
* 对每条关系 ``(u -> v, min_gap=g)``，``t[v] >= t[u] + g``。

全部约束均为下界，因此逐点最早时刻等价于以"虚拟源点"（向每个点连权为
基础下界的边）为起点的最长路。最长路可用 Bellman-Ford 式的逐轮松弛求解，
无需枚举任何候选时间：

* 轮次与边的排列顺序无关，只影响收敛快慢，因此同一批规则无论怎样排列，
  得到的时间表完全相同；
* 若经过 ``n`` 轮完整松弛后仍可松弛，则存在源点可达的总权为正的有向环
  （正权环 => 时刻可沿环无限增长），返回 ``positive_cycle``；
* 否则结果即同时满足全部下界的最早时刻，再逐点检查 ``latest`` 上界。
"""

from __future__ import annotations

# 推演结果状态码（与 HTTP 响应中的稳定错误码保持一致）
STATUS_OK = "ok"
STATUS_POSITIVE_CYCLE = "positive_cycle"
STATUS_DEADLINE_EXCEEDED = "deadline_exceeded"


def earliest_schedule(points, relations, delay=None):
    """求逐点最早可执行时刻。

    :param points: 已校验的提示点列表，元素为
        ``{"id": str, "release": int, "latest": Optional[int]}``。
    :param relations: 已校验的关系列表，元素为
        ``{"from": str, "to": str, "min_gap": int}``。
    :param delay: 可选的 ``{点 ID: 延误覆盖值}``，覆盖值不小于原 release。
    :returns:
        成功::

            {"status": "ok", "times": {id: int, ...}}  # 含全部提示点

        存在总间隔为正的有向环::

            {"status": "positive_cycle"}

        无正权环但有点最早时刻超过 latest::

            {"status": "deadline_exceeded",
             "violations": [{"id": str, "earliest": int, "latest": int}, ...]}
    """
    delay = delay or {}
    ids = [p["id"] for p in points]
    n = len(ids)

    # 基础下界：虚拟源点 -> 点 p，权为 release（或 delay 覆盖）。
    times = {}
    for p in points:
        pid = p["id"]
        times[pid] = int(delay.get(pid, p["release"]))

    latest_by_id = {p["id"]: p.get("latest") for p in points}

    # 边以 (u, v, gap) 表示 t[v] >= t[u] + gap。
    edges = [(r["from"], r["to"], r["min_gap"]) for r in relations]

    # 至多 n 轮松弛即可得到无正权环图上的最长路。
    for _ in range(n):
        changed = False
        for u, v, gap in edges:
            candidate = times[u] + gap
            if candidate > times[v]:
                times[v] = candidate
                changed = True
        if not changed:
            break

    # 第 n+1 轮检测：仍能松弛 => 存在总权为正的有向环。
    for u, v, gap in edges:
        if times[u] + gap > times[v]:
            return {"status": STATUS_POSITIVE_CYCLE}

    # 无正权环：逐点核对 latest 上界（按 ID 升序，输出确定）。
    violations = []
    for pid in ids:
        latest = latest_by_id[pid]
        if latest is not None and times[pid] > latest:
            violations.append(
                {"id": pid, "earliest": times[pid], "latest": latest}
            )
    if violations:
        return {"status": STATUS_DEADLINE_EXCEEDED, "violations": violations}

    return {"status": STATUS_OK, "times": times}
