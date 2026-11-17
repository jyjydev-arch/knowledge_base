"""应用入口

在这里统一注册路由、跨域中间件，以及最重要的「统一返回格式」异常处理器：
    业务异常 / 参数校验失败 / 404 / 500 都会被包成
    {"status": <和HTTP一致的码>, "msg": <提示语>, "data": <数据或null>}
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.documentrouter import router as document_router
from app.api.v1.kbrouter import router as kb_router
from app.api.v1.userrouter import router as user_router
from app.core import startup
from app.core.config import settings
from app.core.exceptions import BusinessError
from app.schemas.common import ApiResponse


async def prepare_schema() -> dict:
    """启动期检查（单独抽出来，测试才能不打真全局引擎就验到三种策略）"""
    return await startup.ensure_schema_on_startup()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 内存库/未配库会在 startup 里短路，所以测试车道（with TestClient）不会因此多连一次库
    await prepare_schema()
    yield


app = FastAPI(
    lifespan=lifespan,
    title=settings.APP_NAME,
    description=settings.APP_DESCRIPTION,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ---------------- 跨域 ----------------
_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials="*" not in _origins,  # "*" 时浏览器不允许同时携带 cookie
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- 统一返回格式 ----------------
def _envelope(status: int, msg: str, data=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"status": status, "msg": msg, "data": data},
    )


@app.exception_handler(BusinessError)
async def business_error_handler(request: Request, exc: BusinessError):
    """业务异常 -> {"status": 4xx, "msg": "...", "data": null}"""
    return _envelope(exc.status, exc.msg, exc.data)


def _clean_errors(errors: list[dict]) -> list[dict]:
    """pydantic 的 errors() 里 ctx 可能带异常对象，不能直接 json 序列化"""
    cleaned = []
    for err in errors:
        cleaned.append(
            {
                "field": ".".join(str(part) for part in err.get("loc", [])[1:]) or "body",
                "location": str(err.get("loc", [""])[0] or "body"),
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
        )
    return cleaned


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """参数校验失败 -> 422，msg 里给出第一个错误提示，data 里放明细"""
    errors = _clean_errors(exc.errors())
    first = errors[0] if errors else {"field": "body", "msg": "请求参数不合法"}
    msg = f"参数校验失败: {first['field']} {first['msg']}".strip()
    return _envelope(422, msg, {"errors": errors})


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    """404 路由不存在 / 405 方法不对等框架异常，同样用统一格式返回"""
    return _envelope(exc.status_code, str(exc.detail), None)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """兜底：数据库挂了之类的意外错误也不要吐 HTML 500"""
    if settings.DEBUG:
        return _envelope(500, f"服务器内部错误: {type(exc).__name__}: {exc}", None)
    return _envelope(500, "服务器繁忙，请稍后重试", None)


# ---------------- 路由（以后新增模块就再多 import 一个 router 多 include 一次） ----------------
app.include_router(user_router, prefix=settings.API_V1_PREFIX)
app.include_router(document_router, prefix=settings.API_V1_PREFIX)
app.include_router(kb_router, prefix=settings.API_V1_PREFIX)


@app.get("/", response_model=ApiResponse[dict], summary="根路径", tags=["Meta"])
async def root():
    return ApiResponse.ok({"docs": "/docs", "api": settings.API_V1_PREFIX}, msg="AI知识库系统")


@app.get(
    "/health",
    response_model=ApiResponse[dict],
    summary="健康检查（含数据库探活）",
    tags=["Meta"],
)
async def health():
    """真的连一次库，而不是只报版本号

    状态码必须是 503：Docker healthcheck / k8s readiness / CI 只看状态码，不看 body。
    响应体只给类别（dialect、缺哪几张表），**不给连接串和异常原文** ——
    里面含用户名密码和内网地址，`/health` 通常是无需鉴权就能访问的。
    """
    code, data = await startup.health_payload()
    error = (data.get("database") or {}).get("error")
    if code == 200:
        return ApiResponse.ok(data, msg="服务正常")
    if (data.get("database") or {}).get("ephemeral"):
        return _envelope(code, "服务正常（内存库：测试/临时车道，不代表持久化状态）", data)
    if error == startup.ERROR_MISSING_TABLES:
        return _envelope(code, "数据库可连但表不完整：请先运行 python scripts/init_db.py", data)
    if error == startup.ERROR_NO_METADATA:
        return _envelope(code, "ORM 元数据为空，检查 app/db/models.py 是否被导入", data)
    return _envelope(code, "数据库不可用：请检查 DATABASE_URL 与 PostgreSQL 是否在运行", data)
