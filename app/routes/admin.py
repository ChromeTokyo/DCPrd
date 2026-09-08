"""用户管理 / 系统设置 / 审计日志。"""
from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db
from ..main import get_ctx
from ..security import new_invite_code
from ..web import Ctx

router = APIRouter(prefix="/admin")


def _user_or_404(ctx: Ctx, user_id: int):
    row = db.one(ctx.conn, "SELECT * FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,))
    if not row:
        raise HTTPException(404, "用户不存在")
    return row


@router.get("/users")
async def users_page(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    users = db.all_rows(
        ctx.conn,
        "SELECT * FROM users WHERE deleted_at IS NULL ORDER BY CASE role WHEN 'super' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, name",
    )
    return ctx.render("admin_users.html", users=users)


@router.post("/users/new")
async def users_new(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    name = str(form.get("name") or "").strip()
    if not name:
        ctx.flash("err", "请输入成员名称")
        return ctx.redirect("/admin/users")
    now = db.utcnow()
    uid = db.insert(
        ctx.conn, "users",
        {"name": name, "role": "user", "invite_code": new_invite_code(), "invite_created_at": now, "created_by": ctx.user["id"], "created_at": now},
    )
    db.audit(ctx.conn, ctx.user, "create_user", "user", uid, {"name": name})
    ctx.flash("ok", f"已创建用户「{name}」，请复制邀请链接发给对方")
    return ctx.redirect("/admin/users")


@router.post("/users/{user_id}/regen-invite")
async def users_regen(user_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    u = _user_or_404(ctx, user_id)
    if u["tg_id"] is not None:
        ctx.flash("err", "该用户已绑定 Telegram，请先解绑")
        return ctx.redirect("/admin/users")
    db.update(ctx.conn, "users", user_id, {"invite_code": new_invite_code(), "invite_created_at": db.utcnow()})
    db.audit(ctx.conn, ctx.user, "regen_invite", "user", user_id, {"name": u["name"]})
    ctx.flash("ok", "邀请链接已重新生成，旧链接失效")
    return ctx.redirect("/admin/users")


@router.post("/users/{user_id}/unbind")
async def users_unbind(user_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    u = _user_or_404(ctx, user_id)
    if u["role"] == "super" and u["id"] != ctx.user["id"]:
        raise HTTPException(403, "不能解绑超级管理员")
    db.update(
        ctx.conn, "users", user_id,
        {"tg_id": None, "tg_username": None, "tg_first_name": None, "tg_last_name": None, "tg_photo_url": None, "bound_at": None,
         "invite_code": new_invite_code(), "invite_created_at": db.utcnow()},
    )
    ctx.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    db.audit(ctx.conn, ctx.user, "unbind_user", "user", user_id, {"name": u["name"], "tg_id": u["tg_id"]})
    ctx.flash("ok", f"已解绑「{u['name']}」的 Telegram，并生成了新的邀请链接")
    if user_id == ctx.user["id"]:
        return ctx.redirect("/login")
    return ctx.redirect("/admin/users")


@router.post("/users/{user_id}/delete")
async def users_delete(user_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    u = _user_or_404(ctx, user_id)
    if u["role"] == "super":
        raise HTTPException(403, "不能删除超级管理员")
    if u["id"] == ctx.user["id"]:
        raise HTTPException(400, "不能删除自己")
    db.update(ctx.conn, "users", user_id, {"deleted_at": db.utcnow(), "deleted_by": ctx.user["id"], "tg_id": None, "invite_code": None})
    ctx.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    ctx.conn.execute("DELETE FROM requirement_owners WHERE user_id = ?", (user_id,))
    db.audit(ctx.conn, ctx.user, "delete_user", "user", user_id, {"name": u["name"], "tg_id": u["tg_id"]})
    ctx.flash("ok", f"用户「{u['name']}」已删除")
    return ctx.redirect("/admin/users")


@router.post("/users/{user_id}/toggle-admin")
async def users_toggle_admin(user_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_super()
    form = await request.form()
    ctx.check_csrf(form)
    u = _user_or_404(ctx, user_id)
    if u["role"] == "super":
        raise HTTPException(400, "超级管理员不能被降级")
    new_role = "user" if u["role"] == "admin" else "admin"
    db.update(ctx.conn, "users", user_id, {"role": new_role})
    db.audit(ctx.conn, ctx.user, "toggle_admin", "user", user_id, {"name": u["name"], "from": u["role"], "to": new_role})
    ctx.flash("ok", f"「{u['name']}」已{'设为管理员' if new_role == 'admin' else '取消管理员'}")
    return ctx.redirect("/admin/users")


@router.get("/settings")
async def settings_page(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    return ctx.render("admin_settings.html")


@router.post("/settings")
async def settings_save(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    site_name = str(form.get("site_name") or "").strip() or ctx.cfg.site_name
    jira = str(form.get("jira_base_url") or "").strip().rstrip("/") or ctx.cfg.jira_base_url
    tz = str(form.get("timezone") or "").strip() or "Asia/Tokyo"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        ctx.flash("err", f"无效的时区：{tz}")
        return ctx.redirect("/admin/settings")
    try:
        mb = int(str(form.get("max_upload_mb") or "300"))
        if not 1 <= mb <= 4000:
            raise ValueError
    except ValueError:
        ctx.flash("err", "上传上限需为 1～4000 之间的整数（MB）")
        return ctx.redirect("/admin/settings")
    sandbox = "1" if form.get("sandbox_enabled") else "0"
    new_values = {"site_name": site_name, "jira_base_url": jira, "timezone": tz, "max_upload_mb": str(mb), "sandbox_enabled": sandbox}
    changed = {k: {"from": ctx.settings.get(k), "to": v} for k, v in new_values.items() if ctx.settings.get(k) != v}
    for k, v in new_values.items():
        db.set_setting(ctx.conn, k, v)
    if changed:
        db.audit(ctx.conn, ctx.user, "update_settings", "settings", None, changed)
    ctx.flash("ok", "设置已保存")
    return ctx.redirect("/admin/settings")


@router.get("/audit")
async def audit_page(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    logs = db.all_rows(ctx.conn, "SELECT * FROM audit_log ORDER BY id DESC LIMIT 500")
    return ctx.render("admin_audit.html", logs=logs)
