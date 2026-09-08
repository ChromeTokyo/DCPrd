"""子文档详情 / 编辑 / 删除 / 重置分享 / 上传。"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.concurrency import run_in_threadpool

from .. import db, queries
from ..main import get_ctx
from ..security import new_share_code
from ..storage import UploadError, copy_stream_limited, create_version_from_upload
from ..web import Ctx

router = APIRouter()


async def handle_upload(ctx: Ctx, doc, upload: UploadFile | None, note: str) -> tuple[int, bool] | None:
    """处理一次上传；无文件返回 None。UploadError 由调用方转成 flash。"""
    if upload is None or not upload.filename:
        return None
    tmp = ctx.cfg.tmp_dir / f"up-{uuid.uuid4().hex}"
    ctx.cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        size = await run_in_threadpool(copy_stream_limited, upload.file, tmp, ctx.max_upload_bytes)
        result = await run_in_threadpool(
            create_version_from_upload,
            ctx.cfg, ctx.conn, doc["id"], ctx.user["id"], upload.filename, tmp, size, note or "", ctx.max_upload_bytes,
        )
    finally:
        Path(tmp).unlink(missing_ok=True)
        await upload.close()
    db.update(ctx.conn, "documents", doc["id"], {"updated_at": db.utcnow()})
    queries.touch_requirement(ctx.conn, doc["requirement_id"], ctx.user["id"])
    return result


def back_url(doc, req) -> str:
    return f"/req/{req['id']}" if req["kind"] == "single" else f"/doc/{doc['id']}"


def can_delete(ctx: Ctx, row) -> bool:
    return ctx.is_admin or row["created_by"] == ctx.user["id"]


@router.get("/doc/{doc_id}")
async def doc_detail(doc_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    doc, req = queries.document_or_404(ctx.conn, doc_id)
    if req["kind"] == "single":
        return ctx.redirect(f"/req/{req['id']}")
    return ctx.render(
        "doc_detail.html",
        doc=doc,
        req=req,
        versions=queries.versions_of(ctx.conn, doc["id"]),
        latest=queries.latest_version(ctx.conn, doc["id"]),
        can_delete=can_delete(ctx, doc),
        creator=queries.user_name(ctx.conn, doc["created_by"]),
    )


@router.post("/doc/{doc_id}/edit")
async def doc_edit(doc_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    doc, req = queries.document_or_404(ctx.conn, doc_id)
    name = (form.get("name") or "").strip()
    if not name:
        ctx.flash("err", "名称不能为空")
        return ctx.redirect(back_url(doc, req))
    db.update(ctx.conn, "documents", doc["id"], {"name": name, "notes": (form.get("notes") or "").strip(), "updated_at": db.utcnow()})
    queries.touch_requirement(ctx.conn, req["id"], ctx.user["id"])
    ctx.flash("ok", "已保存")
    return ctx.redirect(back_url(doc, req))


@router.post("/doc/{doc_id}/delete")
async def doc_delete(doc_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    doc, req = queries.document_or_404(ctx.conn, doc_id)
    if req["kind"] == "single":
        raise HTTPException(400, "单体需求请直接删除需求")
    if not can_delete(ctx, doc):
        raise HTTPException(403, "只能删除自己创建的子文档")
    db.update(ctx.conn, "documents", doc["id"], {"deleted_at": db.utcnow(), "deleted_by": ctx.user["id"]})
    queries.touch_requirement(ctx.conn, req["id"], ctx.user["id"])
    db.audit(ctx.conn, ctx.user, "delete_document", "document", doc["id"], {"name": doc["name"], "requirement_id": req["id"]})
    ctx.flash("ok", f"子文档「{doc['name']}」已删除")
    return ctx.redirect(f"/req/{req['id']}")


@router.post("/doc/{doc_id}/reset-share")
async def doc_reset_share(doc_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    doc, req = queries.document_or_404(ctx.conn, doc_id)
    code = new_share_code(ctx.conn)
    db.update(ctx.conn, "documents", doc["id"], {"share_code": code, "updated_at": db.utcnow()})
    db.audit(ctx.conn, ctx.user, "reset_share", "document", doc["id"], {"old": doc["share_code"], "new": code})
    ctx.flash("ok", "分享链接已重置，旧链接已失效")
    return ctx.redirect(back_url(doc, req))


@router.post("/doc/{doc_id}/upload")
async def doc_upload(doc_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    doc, req = queries.document_or_404(ctx.conn, doc_id)
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        ctx.flash("err", "请选择要上传的文件")
        return ctx.redirect(back_url(doc, req))
    try:
        result = await handle_upload(ctx, doc, upload, str(form.get("note") or ""))
    except UploadError as e:
        ctx.flash("err", str(e))
        return ctx.redirect(back_url(doc, req))
    vid, needs_entry = result
    if needs_entry:
        ctx.flash("ok", "上传成功，请选择入口文件")
        return ctx.redirect(f"/ver/{vid}/entry")
    ctx.flash("ok", "上传成功，已发布为最新版本")
    return ctx.redirect(back_url(doc, req))
