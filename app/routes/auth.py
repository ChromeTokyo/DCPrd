"""登录 / 邀请 / Telegram 回调 / 退出。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..main import get_ctx
from ..security import verify_telegram_auth
from ..web import INVITE_COOKIE, Ctx, safe_next

router = APIRouter()

TG_FIELDS = ("id", "first_name", "last_name", "username", "photo_url", "auth_date", "hash")


def _has_bound_users(ctx: Ctx) -> bool:
    return db.one(ctx.conn, "SELECT 1 FROM users WHERE tg_id IS NOT NULL AND deleted_at IS NULL LIMIT 1") is not None


@router.get("/login")
async def login_page(request: Request, ctx: Ctx = Depends(get_ctx), next: str | None = None, error: str | None = None):
    if ctx.user:
        return ctx.redirect(safe_next(next))
    return ctx.render(
        "login.html",
        init_mode=not _has_bound_users(ctx),
        error=error,
        next=safe_next(next),
        auth_url=f"{ctx.base_url}/auth/telegram",
    )


@router.get("/invite/{code}")
async def invite_page(code: str, ctx: Ctx = Depends(get_ctx)):
    invited = db.one(ctx.conn, "SELECT * FROM users WHERE invite_code = ? AND deleted_at IS NULL AND tg_id IS NULL", (code,))
    if not invited:
        return ctx.render("invite.html", invited=None, status_code=404)
    resp = ctx.render("invite.html", invited=invited, auth_url=f"{ctx.base_url}/auth/telegram")
    resp.set_cookie(INVITE_COOKIE, ctx.signer.dumps(code, salt="invite"), max_age=600, httponly=True, samesite="lax", secure=not ctx.cfg.dev_mode, path="/")
    return resp


@router.get("/auth/telegram")
async def telegram_callback(request: Request, ctx: Ctx = Depends(get_ctx)):
    data = {k: v for k, v in request.query_params.items() if k in TG_FIELDS}
    ok, reason = verify_telegram_auth(ctx.cfg.tg_bot_token, data)
    if not ok:
        return ctx.render("login.html", init_mode=not _has_bound_users(ctx), error=reason, next="/", auth_url=f"{ctx.base_url}/auth/telegram", status_code=403)

    tg_id = int(data["id"])
    profile = {
        "tg_username": data.get("username"),
        "tg_first_name": data.get("first_name"),
        "tg_last_name": data.get("last_name"),
        "tg_photo_url": data.get("photo_url"),
    }
    invite_code = ctx.signer.loads(request.cookies.get(INVITE_COOKIE), salt="invite", max_age=600)
    existing = db.one(ctx.conn, "SELECT * FROM users WHERE tg_id = ? AND deleted_at IS NULL", (tg_id,))

    def finish(user_id: int) -> RedirectResponse:
        ctx.login(user_id)
        resp = ctx.redirect("/", status_code=302)
        resp.delete_cookie(INVITE_COOKIE, path="/")
        return resp

    if invite_code:
        invited = db.one(ctx.conn, "SELECT * FROM users WHERE invite_code = ? AND deleted_at IS NULL AND tg_id IS NULL", (invite_code,))
        if invited:
            if existing:
                return ctx.render("invite.html", invited=invited, error=f"该 Telegram 账号已绑定用户「{existing['name']}」，不能重复绑定。", status_code=409)
            role = invited["role"] if _has_bound_users(ctx) else "super"
            db.update(ctx.conn, "users", invited["id"], {"tg_id": tg_id, **profile, "invite_code": None, "bound_at": db.utcnow(), "role": role})
            ctx.flash("ok", f"绑定成功，欢迎 {invited['name']}")
            return finish(invited["id"])
        # 邀请码无效则回落到普通登录流程

    if existing:
        db.update(ctx.conn, "users", existing["id"], profile)
        return finish(existing["id"])

    if not _has_bound_users(ctx):
        # 系统初始化：第一个登录者成为超级管理员
        name = " ".join(x for x in (data.get("first_name"), data.get("last_name")) if x) or data.get("username") or f"tg{tg_id}"
        uid = db.insert(
            ctx.conn,
            "users",
            {"name": name, "role": "super", "tg_id": tg_id, **profile, "bound_at": db.utcnow(), "created_at": db.utcnow()},
        )
        db.audit(ctx.conn, db.one(ctx.conn, "SELECT * FROM users WHERE id = ?", (uid,)), "init_super", "user", uid, {"tg_id": tg_id})
        ctx.flash("ok", "系统初始化完成，你已成为超级管理员")
        return finish(uid)

    return ctx.render(
        "login.html", init_mode=False, error="该 Telegram 尚未被邀请，请联系管理员", next="/", auth_url=f"{ctx.base_url}/auth/telegram", status_code=403
    )


@router.post("/logout")
async def logout(request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await request.form()
    if ctx.user:
        ctx.check_csrf(form)
        ctx.logout()
    return ctx.redirect("/login")
