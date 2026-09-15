"""通知中心、我的提醒（偏好）、Telegram Webhook、Bot 关注检测。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import db
from ..main import get_ctx
from ..notify import KINDS, bot_following, mark_bot_blocked, mark_bot_started, mark_read, notify_user, set_prefs, user_prefs
from ..web import Ctx

router = APIRouter()
log = logging.getLogger("dcpm.tg")


# ---------- 通知中心 ----------

@router.get("/notifications")
async def notifications_page(ctx: Ctx = Depends(get_ctx), all: int = 0):
    ctx.require_user()
    where = "" if all else " AND read_at IS NULL"
    rows = db.all_rows(ctx.conn, f"SELECT * FROM notifications WHERE user_id = ?{where} ORDER BY id DESC LIMIT 200", (ctx.user["id"],))
    return ctx.render("notifications.html", items=rows, show_all=bool(all), KINDS=KINDS, following=bot_following(ctx.user))


@router.get("/notifications/{nid}/go")
async def notification_go(nid: int, ctx: Ctx = Depends(get_ctx)):
    """点击通知：标已读并跳到目标。"""
    ctx.require_user()
    n = mark_read(ctx.conn, ctx.notifier, nid, ctx.user["id"])
    if not n:
        raise HTTPException(404, "通知不存在")
    url = n["url"] or "/notifications"
    return ctx.redirect(url if url.startswith("/") else "/notifications")


@router.post("/notifications/{nid}/read")
async def notification_read(nid: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    if not mark_read(ctx.conn, ctx.notifier, nid, ctx.user["id"]):
        raise HTTPException(404, "通知不存在")
    return ctx.redirect(str(form.get("back") or "/notifications"))


@router.post("/notifications/read-all")
async def notifications_read_all(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    ctx.conn.execute("UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL", (db.utcnow(), ctx.user["id"]))
    ctx.flash("ok", "已全部标为已读")
    return ctx.redirect("/notifications")


# ---------- 我的提醒 ----------

@router.get("/me/notifications")
async def prefs_page(ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    return ctx.render("prefs.html", prefs=user_prefs(ctx.conn, ctx.user["id"]), KINDS=KINDS, following=bot_following(ctx.user), bot_username=ctx.cfg.tg_bot_username)


@router.post("/me/notifications")
async def prefs_save(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    set_prefs(ctx.conn, ctx.user["id"], {k: bool(form.get(f"kind_{k}")) for k in KINDS})
    ctx.flash("ok", "提醒设置已保存")
    return ctx.redirect("/me/notifications")


@router.post("/me/bot-check")
async def my_bot_check(request: Request, ctx: Ctx = Depends(get_ctx)):
    """本人手动检测是否已关注 Bot。"""
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    res = await run_in_threadpool(ctx.notifier.chat_exists, ctx.user["tg_id"])
    if res:
        mark_bot_started(ctx.conn, ctx.user["tg_id"])
        ctx.flash("ok", "已检测到你关注了 Bot，通知可以送达")
    elif res is False:
        mark_bot_blocked(ctx.conn, ctx.user["tg_id"])
        ctx.flash("err", f"还没有和 Bot 建立对话：请在 Telegram 打开 @{ctx.cfg.tg_bot_username} 点 Start 后再检测")
    else:
        ctx.flash("err", "检测失败（网络或 Bot 配置问题），请稍后重试")
    return ctx.redirect(str(form.get("back") or "/me/notifications"))


# ---------- 管理员：批量检测 ----------

@router.post("/admin/users/bot-check")
async def admin_bot_check(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    users = db.all_rows(ctx.conn, "SELECT id, tg_id FROM users WHERE deleted_at IS NULL AND tg_id IS NOT NULL")
    ok = bad = unknown = 0
    for u in users:
        res = await run_in_threadpool(ctx.notifier.chat_exists, u["tg_id"])
        if res:
            mark_bot_started(ctx.conn, u["tg_id"]); ok += 1
        elif res is False:
            mark_bot_blocked(ctx.conn, u["tg_id"]); bad += 1
        else:
            unknown += 1
    ctx.flash("ok", f"检测完成：已关注 {ok}，未关注 {bad}" + (f"，无法判断 {unknown}" if unknown else ""))
    return ctx.redirect("/admin/users")


# ---------- Telegram Webhook ----------

@router.post("/tg/webhook")
async def telegram_webhook(request: Request, ctx: Ctx = Depends(get_ctx)):
    if request.headers.get("x-telegram-bot-api-secret-token") != ctx.notifier.webhook_secret():
        raise HTTPException(403, "bad secret")
    try:
        upd = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True})
    msg = upd.get("message") or upd.get("edited_message")
    if msg and msg.get("chat", {}).get("type") == "private":
        tg_id = msg["chat"]["id"]
        uid = mark_bot_started(ctx.conn, tg_id)
        text = (msg.get("text") or "").strip()
        if text.startswith("/start") or text.startswith("/help"):
            site = ctx.settings.get("site_name") or "DCPrd"
            reply = (f"已连接。{site} 会在这里给你推送需求更新、留言与重点事项提醒，消息下方点「已读」即可确认。\n通知偏好：{ctx.base_url}/me/notifications"
                     if uid else f"你的 Telegram 尚未绑定 {site} 账号。请先用邀请链接在 {ctx.base_url} 登录一次，之后通知会送到这里。")
            await run_in_threadpool(ctx.notifier.send, tg_id, reply)
    cq = upd.get("callback_query")
    if cq:
        data = cq.get("data") or ""
        from_id = cq.get("from", {}).get("id")
        if data.startswith("read:") and data[5:].isdigit():
            n = db.one(ctx.conn, "SELECT n.* FROM notifications n JOIN users u ON u.id = n.user_id WHERE n.id = ? AND u.tg_id = ?", (int(data[5:]), from_id))
            if n:
                mark_read(ctx.conn, ctx.notifier, n["id"])
                await run_in_threadpool(ctx.notifier.answer_callback, cq.get("id"), "已标为已读")
                if n["tg_message_id"]:
                    await run_in_threadpool(ctx.notifier.mark_message_read, from_id, n["tg_message_id"])
            else:
                await run_in_threadpool(ctx.notifier.answer_callback, cq.get("id"), "通知不存在")
        else:
            await run_in_threadpool(ctx.notifier.answer_callback, cq.get("id"), "")
    mcm = upd.get("my_chat_member")
    if mcm and mcm.get("chat", {}).get("type") == "private":
        status = mcm.get("new_chat_member", {}).get("status")
        if status in ("kicked", "left"):
            mark_bot_blocked(ctx.conn, mcm["chat"]["id"])
        elif status == "member":
            mark_bot_started(ctx.conn, mcm["chat"]["id"])
    return JSONResponse({"ok": True})
