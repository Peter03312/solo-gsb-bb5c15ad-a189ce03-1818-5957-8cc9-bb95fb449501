"""FastAPI 应用入口。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
页面：/            API 文档：/api/docs
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .api import router as api_router
from .database import init_db
from .errors import DomainError, NotFoundError
from .web import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if config.SEED_DEMO:
        try:
            from .seed import seed_demo_data
            seed_demo_data()
        except Exception as exc:  # 演示数据失败不影响主服务
            print(f"[seed] 演示数据初始化跳过：{exc}")
    yield


app = FastAPI(
    title="博物馆借展运输环境风险核验系统",
    description="批次 / 有序节点 / 温湿度限值 / 采样间隔 / 带时间戳读数 / "
                "按时间核验 / 缺测中断 / 风险事件闭环 / 评估版本留痕",
    version="1.0.0",
    docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api_router)
app.include_router(web_router)


@app.get("/healthz", tags=["系统"])
def healthz():
    return {"status": "ok"}


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    if _is_api(request):
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return _html_error(request, exc, 400)


@app.exception_handler(NotFoundError)
async def not_found_handler(request: Request, exc: NotFoundError):
    if _is_api(request):
        return JSONResponse(status_code=404, content={"error": str(exc)})
    return _html_error(request, exc, 404)


def _html_error(request: Request, exc: Exception, status: int):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory="app/templates")
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "status": status,
         "message": str(exc), "api_docs": "/api/docs"},
        status_code=status)
