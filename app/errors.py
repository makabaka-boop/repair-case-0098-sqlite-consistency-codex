"""稳定错误码与统一错误响应。

所有失败响应均为 ``{"error": {"code": <稳定代码>, "message": <人类可读说明>,
"details": <可选补充>}}``，代码字符串即接口契约的一部分，不随实现改动。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """业务层抛出的、带稳定代码的错误。"""

    def __init__(self, code: str, message: str, status_code: int = 400,
                 details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    def to_response(self) -> JSONResponse:
        body = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            body["error"]["details"] = self.details
        return JSONResponse(status_code=self.status_code, content=body)


# ---- 稳定错误代码 ---------------------------------------------------------

INVALID_TEMPLATE = "invalid_template"        # 模板不满足登记规则，HTTP 400
INVALID_DELAY = "invalid_delay"              # delay 覆盖不合法，HTTP 400
TEMPLATE_NOT_FOUND = "template_not_found"    # 模板 ID 不存在，HTTP 404
RESULT_NOT_FOUND = "result_not_found"        # 结果 ID 不存在，HTTP 404
POSITIVE_CYCLE = "positive_cycle"            # 存在正权环，HTTP 422
DEADLINE_EXCEEDED = "deadline_exceeded"      # 最早时刻超过 latest，HTTP 422
BAD_JSON = "bad_json"                        # 请求体不是合法 JSON，HTTP 400
NOT_FOUND = "not_found"                      # 路径不存在，HTTP 404
METHOD_NOT_ALLOWED = "method_not_allowed"    # HTTP 方法不允许，HTTP 405


def register_exception_handlers(app) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(_: Request,
                                        exc: RequestValidationError):
        # FastAPI 自身的解析/校验失败（如请求体根本不是 JSON 类型）统一归入
        # bad_json，保证对外代码稳定。
        return AppError(
            BAD_JSON, "Request body must be valid JSON of the documented shape.",
            status_code=400, details=exc.errors(),
        ).to_response()

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return AppError(NOT_FOUND, "Resource not found.",
                            status_code=404).to_response()
        if exc.status_code == 405:
            return AppError(METHOD_NOT_ALLOWED,
                            "HTTP method not allowed for this path.",
                            status_code=405).to_response()
        return AppError("http_error", str(exc.detail),
                        status_code=exc.status_code).to_response()
