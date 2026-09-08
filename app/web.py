"""请求上下文、模板渲染、登录态、CSRF、flash 等 Web 层公共设施。"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from . import db
from .config import Config
from .security import Signer, new_csrf, new_session_id

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
SESSION_COOKIE = "dcpm_sid"
FLASH_COOKIE = "dcpm_flash"
INVITE_COOKIE = "dcpm_invite"
# 会话永不过期：只有手动退出、被解绑/删除用户才失效。Cookie 按浏览器上限（400 天）设置并在每次访问时滑动续期。
COOKIE_MAX_AGE = 400 * 86400
SESSION_FOREVER = "9999-12-31T00:00:00"
PROJECTS = {"eb": "EB", "im": "IM", "tk": "TK"}

_URL_RE = re.compile(r"(https?://[^\s<>\"']+)")
_JIRA_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*-\d+")


class LoginRedirect(Exception):
    def __init__(self, next_path: str):
        self.next_path = next_path


# ---------- Jinja 环境与过滤器 ----------

def _fmt_size(n: int | None) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _linkify_notes(text: str | None) -> Markup:
    if not text:
        return Markup("")
    out: list[str] = []
    for i, part in enumerate(_URL_RE.split(text)):
        if i % 2 == 1:
            out.append(f'<a href="{escape(part)}" target="_blank" rel="noopener noreferrer">{escape(part)}</a>')
        else:
            out.append(str(escape(part)).replace("\n", "<br>"))
    return Markup("".join(out))


def split_jira_keys(text: str | None) -> list[str]:
    if not text:
        return []
    keys = [k.strip().upper() for k in re.split(r"[\s,，;；]+", text) if k.strip()]
    seen: list[str] = []
    for k in keys:
        if k not in seen:
            seen.append(k)
    return seen


def build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["filesize"] = _fmt_size
    env.filters["linkify"] = _linkify_notes
    env.filters["jira_keys"] = split_jira_keys
    env.filters["urlquote"] = lambda s: quote(str(s), safe="")
    env.filters["tojson_attr"] = lambda v: html.escape(json.dumps(v, ensure_ascii=False), quote=True)
    env.globals["PROJECTS"] = PROJECTS
    return env


# ---------- 请求上下文 ----------

class Ctx:
    """一个请求内共享的对象：配置、DB 连接、设置、当前用户。"""

    def __init__(self, request: Request, cfg: Config, conn: sqlite3.Connection):
        self.request = request
        self.cfg = cfg
        self.conn = conn
        self.settings: dict[str, str] = db.get_settings(conn)
        self.user: sqlite3.Row | None = None
        self.session: sqlite3.Row | None = None
        self._pending_cookies: list[tuple[str, dict[str, Any]]] = []
        self._load_user()

    # --- 设置快捷方式 ---
    @property
    def signer(self) -> Signer:
        return self.request.app.state.signer

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.get("timezone") or self.cfg.timezone)
        except ZoneInfoNotFoundError:
            return ZoneInfo("Asia/Tokyo")

    @property
    def max_upload_bytes(self) -> int:
        try:
            mb = int(self.settings.get("max_upload_mb") or self.cfg.max_upload_mb)
        except ValueError:
            mb = self.cfg.max_upload_mb
        return max(1, mb) * 1024 * 1024

    @property
    def sandbox_enabled(self) -> bool:
        return (self.settings.get("sandbox_enabled") or "1") == "1"

    @property
    def base_url(self) -> str:
        return self.cfg.base_url

    @property
    def is_admin(self) -> bool:
        return bool(self.user) and self.user["role"] in ("admin", "super")

    @property
    def is_super(self) -> bool:
        return bool(self.user) and self.user["role"] == "super"

    # --- 时间 ---
    def fmt_dt(self, iso: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        d = db.parse_utc(iso)
        if not d:
            return ""
        return d.astimezone(self.tz).strftime(fmt)

    # --- 登录态 ---
    def _load_user(self) -> None:
        raw = self.request.cookies.get(SESSION_COOKIE)
        sid = self.signer.loads(raw, salt="session", max_age=None)
        if not sid:
            return
        sess = db.one(self.conn, "SELECT * FROM sessions WHERE id = ?", (sid,))
        if not sess:
            return
        now = db.utcnow()
        user = db.one(self.conn, "SELECT * FROM users WHERE id = ? AND deleted_at IS NULL AND tg_id IS NOT NULL", (sess["user_id"],))
        if not user:
            self.conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            return
        if sess["last_seen"] < (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"):
            self.conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now, sid))
            self._pending_cookies.append((SESSION_COOKIE, self._session_cookie(raw)))  # 滑动续期
        self.user = user
        self.session = sess

    def _session_cookie(self, value: str) -> dict[str, Any]:
        return {"value": value, "max_age": COOKIE_MAX_AGE, "httponly": True, "samesite": "lax", "secure": not self.cfg.dev_mode, "path": "/"}

    def login(self, user_id: int) -> None:
        # 单点登录：同一账号只保留一个有效会话，新登录即踢掉其他设备
        self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        sid = new_session_id()
        db.insert(
            self.conn,
            "sessions",
            {"id": sid, "user_id": user_id, "csrf": new_csrf(), "created_at": db.utcnow(), "expires_at": SESSION_FOREVER, "last_seen": db.utcnow()},
        )
        self._pending_cookies.append((SESSION_COOKIE, self._session_cookie(self.signer.dumps(sid, salt="session"))))

    def logout(self) -> None:
        if self.session:
            self.conn.execute("DELETE FROM sessions WHERE id = ?", (self.session["id"],))
        self._pending_cookies.append((SESSION_COOKIE, {"value": "", "max_age": 0, "path": "/"}))

    @property
    def csrf(self) -> str:
        return self.session["csrf"] if self.session else ""

    def check_csrf(self, form) -> None:
        token = form.get("csrf") if hasattr(form, "get") else None
        if not self.session or not token or token != self.session["csrf"]:
            raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面后重试")

    def require_user(self) -> sqlite3.Row:
        if not self.user:
            path = self.request.url.path
            if self.request.url.query:
                path += "?" + self.request.url.query
            raise LoginRedirect(path)
        return self.user

    def require_admin(self) -> sqlite3.Row:
        self.require_user()
        if not self.is_admin:
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return self.user

    def require_super(self) -> sqlite3.Row:
        self.require_user()
        if not self.is_super:
            raise HTTPException(status_code=403, detail="仅超级管理员可执行此操作")
        return self.user

    # --- flash ---
    def flash(self, kind: str, message: str) -> None:
        self._pending_cookies.append(
            (FLASH_COOKIE, {"value": self.signer.dumps({"t": kind, "m": message}, salt="flash"), "max_age": 60, "httponly": True, "samesite": "lax", "secure": not self.cfg.dev_mode, "path": "/"})
        )

    def _pop_flash(self) -> dict | None:
        raw = self.request.cookies.get(FLASH_COOKIE)
        if not raw:
            return None
        self._pending_cookies.append((FLASH_COOKIE, {"value": "", "max_age": 0, "path": "/"}))
        data = self.signer.loads(raw, salt="flash", max_age=120)
        return data if isinstance(data, dict) else None

    # --- 响应 ---
    def apply_cookies(self, response: Response) -> Response:
        for name, kw in self._pending_cookies:
            if kw.get("max_age") == 0:
                response.delete_cookie(name, path=kw.get("path", "/"))
            else:
                response.set_cookie(name, **kw)
        self._pending_cookies.clear()
        return response

    def redirect(self, url: str, status_code: int = 303) -> RedirectResponse:
        return self.apply_cookies(RedirectResponse(url, status_code=status_code))

    def render(self, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        env: Environment = self.request.app.state.jinja
        context.setdefault("flash", self._pop_flash())
        context.update(
            ctx=self,
            cfg=self.cfg,
            settings=self.settings,
            user=self.user,
            csrf=self.csrf,
            is_admin=self.is_admin,
            is_super=self.is_super,
            base_url=self.base_url,
            site_name=self.settings.get("site_name") or self.cfg.site_name,
            jira_base_url=(self.settings.get("jira_base_url") or self.cfg.jira_base_url).rstrip("/"),
            request=self.request,
            fmt_dt=self.fmt_dt,
        )
        body = env.get_template(template).render(**context)
        resp = HTMLResponse(body, status_code=status_code)
        return self.apply_cookies(resp)


def safe_next(next_path: str | None, default: str = "/") -> str:
    """只接受站内相对路径。"""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//") or "\\" in next_path:
        return default
    return next_path


def user_display(row: sqlite3.Row | None) -> str:
    return row["name"] if row else "（已删除用户）"
