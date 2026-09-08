"""2.0 公开分享：/p2/<需求分享码>/ 目录页、各页面改稿查看（免登录）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from .. import db
from ..main import get_ctx
from ..web import Ctx
from . import queries as q
from .routes import _serve_version

router = APIRouter()


def _req_by_code(ctx: Ctx, code: str):
    row = db.one(ctx.conn, "SELECT * FROM requirements WHERE share_code = ? AND kind = 'v2' AND deleted_at IS NULL", (code,))
    if not row:
        raise HTTPException(404, "链接无效或已失效")
    return row


def _version_allowed(ctx: Ctx, req, version_id: int):
    ver = q.version_or_404(ctx.conn, version_id)
    if ver["requirement_id"] == req["id"]:
        return ver
    # 允许访问需求内改稿的基线版本
    if db.one(ctx.conn, "SELECT 1 FROM page_versions WHERE requirement_id = ? AND base_version_id = ? AND deleted_at IS NULL", (req["id"], version_id)):
        return ver
    if db.one(ctx.conn, "SELECT 1 FROM requirement_pages WHERE requirement_id = ? AND base_version_id = ? AND deleted_at IS NULL", (req["id"], version_id)):
        return ver
    raise HTTPException(404, "版本不属于该需求")


@router.get("/p2/{code}")
async def dir_noslash(code: str, ctx: Ctx = Depends(get_ctx)):
    return ctx.redirect(f"/p2/{code}/", status_code=301)


@router.get("/p2/{code}/")
async def public_dir(code: str, ctx: Ctx = Depends(get_ctx)):
    req = _req_by_code(ctx, code)
    resp = ctx.render("v2/public_dir.html", req=req, pages=q.requirement_pages(ctx.conn, req["id"]), req_apps=q.requirement_apps(ctx.conn, req["id"]))
    resp.headers["cache-control"] = "no-cache"
    return resp


@router.get("/p2/{code}/{page_id}/")
async def public_page(code: str, page_id: int, ctx: Ctx = Depends(get_ctx), v: int | None = None):
    req = _req_by_code(ctx, code)
    page = q.page_or_404(ctx.conn, page_id)
    rp = db.one(ctx.conn, "SELECT * FROM requirement_pages WHERE requirement_id = ? AND page_id = ? AND deleted_at IS NULL", (req["id"], page_id))
    if not rp:
        raise HTTPException(404, "页面不在该需求中")
    proposals = db.all_rows(ctx.conn, "SELECT v.*, u.name AS uploader_name FROM page_versions v LEFT JOIN users u ON u.id = v.uploaded_by WHERE v.requirement_id = ? AND v.page_id = ? AND v.deleted_at IS NULL ORDER BY v.id", (req["id"], page_id))
    selected = next((p for p in proposals if p["id"] == v), None) if v else None
    selected = selected or (proposals[-1] if proposals else None)
    base = None
    diff = None
    if selected and selected["base_version_id"]:
        base = db.one(ctx.conn, "SELECT v.*, r.version AS release_version FROM page_versions v LEFT JOIN releases r ON r.id = v.release_id WHERE v.id = ?", (selected["base_version_id"],))
        diff = json.loads(selected["diff_json"]) if selected["diff_json"] else None
    elif rp["base_version_id"]:
        base = db.one(ctx.conn, "SELECT v.*, r.version AS release_version FROM page_versions v LEFT JOIN releases r ON r.id = v.release_id WHERE v.id = ?", (rp["base_version_id"],))
    resp = ctx.render(
        "v2/public_page.html", req=req, page=page, proposals=proposals, selected=selected, base=base, diff=diff,
        annotations=q.annotations_of(ctx.conn, selected["id"]) if selected else [], others=q.other_pages_in_requirement(ctx.conn, req["id"], page_id),
        menu_path=q.menu_path(ctx.conn, page_id),
    )
    resp.headers["cache-control"] = "no-cache"
    return resp


@router.get("/p2/{code}/c/{version_id}")
async def public_content(code: str, version_id: int, ctx: Ctx = Depends(get_ctx), diff: int = 0):
    req = _req_by_code(ctx, code)
    ver = _version_allowed(ctx, req, version_id)
    return _serve_version(ctx, ver, bool(diff))
