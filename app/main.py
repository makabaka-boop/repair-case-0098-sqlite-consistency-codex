"""FastAPI 入口：模板登记、延误推演、结果查询。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, Request

from . import db
from .errors import (AppError, DEADLINE_EXCEEDED, POSITIVE_CYCLE,
                     RESULT_NOT_FOUND, TEMPLATE_NOT_FOUND,
                     register_exception_handlers)
from .scheduler import (STATUS_DEADLINE_EXCEEDED, STATUS_OK,
                        STATUS_POSITIVE_CYCLE, earliest_schedule)
from .validation import validate_delay, validate_template


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="直播提示点播控服务",
        version="1.0.0",
        description="登记合法提示点模板、推演逐点最早时刻并持久化成功结果。",
    )
    app.state.db_path = db_path
    register_exception_handlers(app)
    db.init_db(db_path)

    def _db_path(request: Request) -> str | None:
        return request.app.state.db_path

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/templates", status_code=201)
    async def register_template(request: Request):
        payload = await _load_json_object(request)
        template = validate_template(payload)
        created_at = _utc_now()
        template_id = db.insert_template(template, _db_path(request))
        return {
            "template_id": template_id,
            "created_at": created_at,
            "point_count": len(template["points"]),
            "relation_count": len(template["relations"]),
            "points": template["points"],
            "relations": template["relations"],
        }

    @app.post("/derivations", status_code=201)
    async def run_derivation(request: Request):
        payload = await _load_json_object(request)

        template_id = payload.get("template_id")
        if not isinstance(template_id, str) or not template_id:
            raise AppError(
                "invalid_delay",
                "Field 'template_id' is required and must be a non-empty string.",
                status_code=400, details={"path": "template_id"})

        template = db.get_template(template_id, _db_path(request))
        if template is None:
            raise AppError(
                TEMPLATE_NOT_FOUND,
                f"Template {template_id!r} does not exist.",
                status_code=404, details={"template_id": template_id})

        delay = validate_delay(payload, template)
        outcome = earliest_schedule(template["points"],
                                    template["relations"], delay)

        if outcome["status"] == STATUS_POSITIVE_CYCLE:
            # 不落库、不触碰既有数据，只给明确的不可实现结论。
            raise AppError(
                POSITIVE_CYCLE,
                "The rules contain a directed cycle with positive total "
                "min_gap; no finite earliest schedule exists.",
                status_code=422)

        if outcome["status"] == STATUS_DEADLINE_EXCEEDED:
            raise AppError(
                DEADLINE_EXCEEDED,
                "At least one point's earliest time exceeds its latest bound.",
                status_code=422,
                details={"violations": outcome["violations"]})

        assert outcome["status"] == STATUS_OK
        times = outcome["times"]
        created_at = _utc_now()
        result_id = db.insert_result(template_id, delay, times,
                                     _db_path(request))
        return {
            "result_id": result_id,
            "template_id": template_id,
            "created_at": created_at,
            "delay": _sorted_dict(delay),
            "times": _sorted_times(template["points"], times),
            "points": _schedule_points(template["points"], times),
        }

    @app.get("/results/{result_id}")
    async def get_result(result_id: str, request: Request):
        record = db.get_result(result_id, _db_path(request))
        if record is None:
            raise AppError(
                RESULT_NOT_FOUND,
                f"Result {result_id!r} does not exist.",
                status_code=404, details={"result_id": result_id})

        template = db.get_template(record["template_id"], _db_path(request))
        # results.template_id 有外键约束，模板必然存在。
        points = template["points"]
        return {
            "result_id": record["id"],
            "template_id": record["template_id"],
            "created_at": record["created_at"],
            "delay": _sorted_dict(record["delay"]),
            "times": _sorted_times(points, record["times"]),
            "points": _schedule_points(points, record["times"]),
        }

    return app


async def _load_json_object(request: Request) -> dict:
    """读取 JSON 请求体；语法错误或非对象统一报 bad_json。"""
    try:
        payload = await request.json()
    except Exception:  # JSONDecodeError 等
        raise AppError(
            "bad_json",
            "Request body is not valid JSON.",
            status_code=400)
    if not isinstance(payload, dict):
        raise AppError(
            "bad_json",
            "Request body must be a JSON object.",
            status_code=400)
    return payload


def _sorted_dict(values: dict) -> dict:
    return {key: values[key] for key in sorted(values)}


def _sorted_times(points, times: dict) -> dict:
    """按提示点 ID 升序输出 {id: 最早时刻}。

    落库模板的 points 已按 ID 升序规范化；模板缺失（理论上不会发生，结果有
    外键约束）时退化为按 times 自身键排序。
    """
    ordered_ids = [p["id"] for p in points] if points else sorted(times)
    return {pid: times[pid] for pid in ordered_ids}


def _schedule_points(points, times: dict) -> list:
    """按提示点 ID 升序输出明细行。"""
    rows = []
    for point in points:
        pid = point["id"]
        rows.append({
            "id": pid,
            "release": point["release"],
            "latest": point["latest"],
            "time": times[pid],
        })
    return rows


app = create_app()
