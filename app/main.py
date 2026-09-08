"""应用入口：create_app()。"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import secrets
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import db
from .config import Config, load_config
from .security import Signer
from .web import BASE_DIR, Ctx, LoginRedirect, build_env

log = logging.getLogger("dcpm")


def get_ctx(request: Request):
    """每请求独立 DB 连接的上下文依赖。"""
    cfg: Config = request.app.state.cfg
    conn = db.connect(cfg.db_path)
    try:
        yield Ctx(request, cfg, conn)
    finally:
        conn.close()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """后台页面加安全头；公开文档路径（/s/、/v/）不加 X-Frame-Options。"""

    async def dispatch(self, request, call_next):
        # 上传上限预检查：超出 Content-Length 的请求在读取正文前拒绝
        if request.method == "POST":
            cl = request.headers.get("content-length")
            if cl and cl.isdigit():
                limit = request.app.state.upload_limit_probe()
                if int(cl) > limit + 2 * 1024 * 1024:
                    return HTMLResponse(f"<h1>413</h1><p>上传内容超过上限（{limit // (1024 * 1024)} MB）。</p>", status_code=413)
        response = await call_next(request)
        path = request.url.path
        if not (path.startswith("/s/") or path.startswith("/v/") or path.startswith("/p2/") or path.startswith("/v2/content/")):
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "same-origin")
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "") or not request.headers.get("accept", "").startswith("application/json")


def _error_page(request: Request, status: int, message: str) -> HTMLResponse:
    env = request.app.state.jinja
    cfg = request.app.state.cfg
    titles = {403: "没有权限", 404: "页面不存在", 413: "文件过大", 400: "请求有误"}
    body = env.get_template("error.html").render(
        status=status, title=titles.get(status, "出错了"), message=message, site_name=cfg.site_name, user=None, base_url=cfg.base_url
    )
    return HTMLResponse(body, status_code=status)


def backup_database(cfg: Config) -> Path | None:
    """用 sqlite3 backup API 生成每日快照，并清理 14 天前的文件。"""
    if not cfg.db_path.exists():
        return None
    cfg.backups_dir.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    dest = cfg.backups_dir / f"dcpm-{today}.sqlite"
    tmp = cfg.backups_dir / f".dcpm-{today}-{os.getpid()}-{secrets.token_hex(4)}.tmp"
    src = sqlite3.connect(str(cfg.db_path), timeout=30)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        src.close()
    tmp.replace(dest)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)
    for f in cfg.backups_dir.glob("dcpm-*.sqlite"):
        try:
            d = dt.datetime.strptime(f.stem[5:], "%Y%m%d").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if d < cutoff:
            f.unlink(missing_ok=True)
    return dest


async def _backup_loop(cfg: Config) -> None:
    while True:
        try:
            await asyncio.to_thread(backup_database, cfg)
        except Exception:  # noqa: BLE001
            log.exception("数据库备份失败")
        await asyncio.sleep(24 * 3600)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    defaults = {
        "site_name": cfg.site_name,
        "jira_base_url": cfg.jira_base_url,
        "max_upload_mb": str(cfg.max_upload_mb),
        "timezone": cfg.timezone,
        "sandbox_enabled": "1",
        "public_widget_enabled": "1",
    }
    db.init_db(cfg.db_path, defaults)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_backup_loop(cfg))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="DCPrd", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.signer = Signer(cfg.secret_key)
    app.state.jinja = build_env()

    def upload_limit_probe() -> int:
        conn = db.connect(cfg.db_path)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = 'max_upload_mb'").fetchone()
        finally:
            conn.close()
        try:
            mb = int(row["value"]) if row else cfg.max_upload_mb
        except (TypeError, ValueError):
            mb = cfg.max_upload_mb
        return max(1, mb) * 1024 * 1024

    app.state.upload_limit_probe = upload_limit_probe
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.exception_handler(LoginRedirect)
    async def _login_redirect(request: Request, exc: LoginRedirect):
        from urllib.parse import quote
        return RedirectResponse(f"/login?next={quote(exc.next_path, safe='')}", status_code=302)

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        if _wants_html(request):
            return _error_page(request, exc.status_code, str(exc.detail))
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        return _error_page(request, 400, "表单内容不完整或格式不正确")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "version": cfg.app_version}

    from .routes import admin, auth, documents, public, requirements, versions

    for mod in (auth, requirements, documents, versions, admin, public):
        app.include_router(mod.router)
    from .v2 import public as v2_public
    from .v2 import routes as v2_routes

    app.include_router(v2_routes.router)
    app.include_router(v2_public.router)
    return app


app = None


def get_app() -> FastAPI:
    """uvicorn 入口：`uvicorn app.main:get_app --factory`。"""
    return create_app()
