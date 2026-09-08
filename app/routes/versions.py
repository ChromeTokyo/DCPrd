"""版本：选择入口 / 重新发布 / 删除（管理员）/ 下载原文件。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .. import db, queries
from ..main import get_ctx
from ..storage import republish_version, version_dir
from ..web import Ctx
from .documents import back_url

router = APIRouter()


@router.get("/ver/{ver_id}/entry")
async def entry_page(ver_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    ver, doc, req = queries.version_or_404(ctx.conn, ver_id)
    candidates = json.loads(ver["html_candidates"] or "[]")
    return ctx.render("ver_entry.html", ver=ver, doc=doc, req=req, candidates=candidates, back=back_url(doc, req))


@router.post("/ver/{ver_id}/entry")
async def entry_set(ver_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    ver, doc, req = queries.version_or_404(ctx.conn, ver_id)
    candidates = json.loads(ver["html_candidates"] or "[]")
    choice = str(form.get("entry") or "")
    if choice not in candidates:
        ctx.flash("err", "请选择一个有效的入口文件")
        return ctx.redirect(f"/ver/{ver_id}/entry")
    db.update(ctx.conn, "versions", ver_id, {"entry_path": choice})
    db.update(ctx.conn, "documents", doc["id"], {"updated_at": db.utcnow()})
    queries.touch_requirement(ctx.conn, req["id"], ctx.user["id"])
    ctx.flash("ok", f"入口已设置为 {choice}，v{ver['number']} 已发布")
    return ctx.redirect(back_url(doc, req))


@router.post("/ver/{ver_id}/republish")
async def republish(ver_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    ver, doc, req = queries.version_or_404(ctx.conn, ver_id)
    if not ver["entry_path"]:
        ctx.flash("err", "该版本尚未选择入口文件，不能重新发布")
        return ctx.redirect(back_url(doc, req))
    new_id = await run_in_threadpool(republish_version, ctx.cfg, ctx.conn, ver, ctx.user["id"])
    db.update(ctx.conn, "documents", doc["id"], {"updated_at": db.utcnow()})
    queries.touch_requirement(ctx.conn, req["id"], ctx.user["id"])
    new = db.one(ctx.conn, "SELECT number FROM versions WHERE id = ?", (new_id,))
    ctx.flash("ok", f"已将 v{ver['number']} 重新发布为 v{new['number']}")
    return ctx.redirect(back_url(doc, req))


@router.post("/ver/{ver_id}/delete")
async def delete_version(ver_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    if not ctx.is_admin:
        raise HTTPException(403, "只有管理员可以删除历史版本")
    ver, doc, req = queries.version_or_404(ctx.conn, ver_id)
    db.update(ctx.conn, "versions", ver_id, {"deleted_at": db.utcnow(), "deleted_by": ctx.user["id"]})
    db.audit(ctx.conn, ctx.user, "delete_version", "version", ver_id, {"document_id": doc["id"], "number": ver["number"], "file": ver["original_filename"]})
    ctx.flash("ok", f"v{ver['number']} 已删除")
    return ctx.redirect(back_url(doc, req))


@router.get("/ver/{ver_id}/download")
async def download_original(ver_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    ver, doc, req = queries.version_or_404(ctx.conn, ver_id)
    path = version_dir(ctx.cfg, doc["id"], ver["number"]) / "original" / ver["original_filename"]
    if not path.is_file():
        raise HTTPException(404, "原文件不存在")
    return FileResponse(str(path), filename=ver["original_filename"], media_type="application/octet-stream")
