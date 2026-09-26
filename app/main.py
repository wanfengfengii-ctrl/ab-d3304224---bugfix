"""深海观测网电缆永久故障归因服务入口。

生产部署（Docker Compose）中 Web 静态页由独立 Nginx 服务托管并反代本 API；
本地无 Nginx 时，若仓库内存在 ``web/`` 目录，本应用也会顺带托管页面，
方便 ``uvicorn app.main:app`` 一键开发。

``/api/health`` 为 API 健康检查端点；``/health`` 为同义的 Web 侧入口。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .routers import router

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(
    title="深海观测网永久故障归因服务",
    version="1.0.0",
    description="枚举永久故障电缆组合，逐轮校验电源可达性并择优归因。",
)

app.include_router(router)


@app.get("/health", tags=["health"])
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "api"})


if WEB_DIR.is_dir() and (WEB_DIR / "index.html").is_file():
    # 本地开发便利：API 进程顺带托管单页（容器内由 Nginx 提供）。

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
