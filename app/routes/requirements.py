"""需求列表 / 新建 / 详情 / 编辑 / 删除 / 转复合 / 重置分享 / 新增子文档。"""
from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import UploadFile

from .. import db, queries
from ..main import get_ctx
from ..security import new_share_code
from ..storage import UploadError
from ..web import PROJECTS, Ctx
from .documents import can_delete, handle_upload

router = APIRouter()
PAGE_SIZE = 50


def _parse_owner_ids(form) -> list[int]:
    ids: list[int] = []
    for v in form.getlist("owners"):
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    return ids


def _set_owners(ctx: Ctx, req_id: int, owner_ids: list[int]) -> None:
    ctx.conn.execute("DELETE FROM requirement_owners WHERE requirement_id = ?", (req_id,))
    for uid in owner_ids:
        if db.one(ctx.conn, "SELECT 1 FROM users WHERE id = ? AND deleted_at IS NULL", (uid,)):
            ctx.conn.execute("INSERT OR IGNORE INTO requirement_owners(requirement_id, user_id) VALUES (?, ?)", (req_id, uid))


@router.get("/")
async def index(ctx: Ctx = Depends(get_ctx), project: str = "", q: str = "", page: int = 1, tag: int | None = None):
    ctx.require_user()
    project = project if project in PROJECTS else ""
    q = (q or "").strip()
    page = max(1, page)
    where = ["r.deleted_at IS NULL", "r.kind != 'v2'"]
    params: list = []
    if project:
        where.append("r.project = ?")
        params.append(project)
    if tag:
        where.append("EXISTS (SELECT 1 FROM requirement_tags rt WHERE rt.requirement_id = r.id AND rt.tag_id = ?)")
        params.append(tag)
    if q:
        like = f"%{q}%"
        where.append(
            "(r.name LIKE ? OR r.jira_keys LIKE ? OR EXISTS (SELECT 1 FROM requirement_owners ro JOIN users u ON u.id = ro.user_id "
            "WHERE ro.requirement_id = r.id AND u.name LIKE ?) OR EXISTS (SELECT 1 FROM requirement_tags rt JOIN tags t ON t.id = rt.tag_id "
            "WHERE rt.requirement_id = r.id AND t.deleted_at IS NULL AND t.name LIKE ?))"
        )
        params += [like, like, like, like]
    where_sql = " AND ".join(where)
    total = db.one(ctx.conn, f"SELECT COUNT(*) AS c FROM requirements r WHERE {where_sql}", params)["c"]
    rows = db.all_rows(
        ctx.conn,
        f"""SELECT r.*,
              (SELECT GROUP_CONCAT(u.name, '、') FROM requirement_owners ro JOIN users u ON u.id = ro.user_id WHERE ro.requirement_id = r.id) AS owner_names,
              (SELECT COUNT(*) FROM documents d WHERE d.requirement_id = r.id AND d.deleted_at IS NULL) AS doc_count
            FROM requirements r WHERE {where_sql}
            ORDER BY r.updated_at DESC, r.id DESC LIMIT ? OFFSET ?""",
        [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    tag_map = queries.tags_for_requirements(ctx.conn, [r["id"] for r in rows])
    items = []
    for r in rows:
        latest = queries.requirement_latest(ctx.conn, r["id"])
        if r["kind"] == "single":
            doc = queries.primary_document(ctx.conn, r["id"])
            share = f"/s/{doc['share_code']}/" if doc else None
        else:
            share = f"/s/{r['share_code']}/"
        items.append({"req": r, "latest": latest, "share": share, "tags": tag_map.get(r["id"], [])})
    return ctx.render(
        "index.html",
        items=items,
        project=project,
        q=q,
        page=page,
        pages=max(1, math.ceil(total / PAGE_SIZE)),
        total=total,
        all_tags=queries.active_tags(ctx.conn),
        tag=tag,
    )


@router.get("/req/new")
async def req_new_page(ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    return ctx.render("req_form.html", req=None, owners=[], users=queries.active_users(ctx.conn), mode="new", all_tags=queries.active_tags(ctx.conn), tag_ids=[])


@router.post("/req/new")
async def req_new(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    project = str(form.get("project") or "")
    kind = str(form.get("kind") or "single")
    name = str(form.get("name") or "").strip()
    if project not in PROJECTS or kind not in ("single", "compound") or not name:
        ctx.flash("err", "请填写项目、类型与名称")
        return ctx.redirect("/req/new")
    now = db.utcnow()
    req_id = db.insert(
        ctx.conn,
        "requirements",
        {
            "project": project,
            "kind": kind,
            "name": name,
            "jira_keys": str(form.get("jira_keys") or "").strip(),
            "notes": str(form.get("notes") or "").strip(),
            "share_code": new_share_code(ctx.conn),
            "created_by": ctx.user["id"],
            "created_at": now,
            "updated_by": ctx.user["id"],
            "updated_at": now,
        },
    )
    _set_owners(ctx, req_id, _parse_owner_ids(form))
    queries.set_tags(ctx.conn, req_id, queries.parse_ids(form, "tags"))
    if kind == "single":
        doc_id = db.insert(
            ctx.conn,
            "documents",
            {"requirement_id": req_id, "name": name, "notes": "", "share_code": new_share_code(ctx.conn), "position": 0,
             "created_by": ctx.user["id"], "created_at": now, "updated_at": now},
        )
        upload = form.get("file")
        if isinstance(upload, UploadFile) and upload.filename:
            doc = db.one(ctx.conn, "SELECT * FROM documents WHERE id = ?", (doc_id,))
            try:
                result = await handle_upload(ctx, doc, upload, str(form.get("note") or ""))
            except UploadError as e:
                ctx.flash("err", f"需求已创建，但文件上传失败：{e}")
                return ctx.redirect(f"/req/{req_id}")
            if result and result[1]:
                ctx.flash("ok", "需求已创建，请选择入口文件")
                return ctx.redirect(f"/ver/{result[0]}/entry")
    ctx.flash("ok", "需求已创建")
    return ctx.redirect(f"/req/{req_id}")


@router.get("/req/{req_id}")
async def req_detail(req_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    req = queries.requirement_or_404(ctx.conn, req_id)
    if req["kind"] == "v2":
        return ctx.redirect(f"/v2/req/{req_id}")
    context = {
        "req": req,
        "owners": queries.owners_of(ctx.conn, req_id),
        "creator": queries.user_name(ctx.conn, req["created_by"]),
        "updater": queries.user_name(ctx.conn, req["updated_by"]),
        "can_delete": can_delete(ctx, req),
        "tags": queries.tags_of(ctx.conn, req_id),
        "all_tags": queries.active_tags(ctx.conn),
    }
    if req["kind"] == "single":
        doc = queries.primary_document(ctx.conn, req_id)
        context.update(doc=doc, versions=queries.versions_of(ctx.conn, doc["id"]) if doc else [], latest=queries.latest_version(ctx.conn, doc["id"]) if doc else None)
    else:
        context.update(docs=queries.documents_of(ctx.conn, req_id))
    return ctx.render("req_detail.html", **context)


@router.get("/req/{req_id}/edit")
async def req_edit_page(req_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    req = queries.requirement_or_404(ctx.conn, req_id)
    return ctx.render("req_form.html", req=req, owners=[o["id"] for o in queries.owners_of(ctx.conn, req_id)], users=queries.active_users(ctx.conn), mode="edit", all_tags=queries.active_tags(ctx.conn), tag_ids=[t["id"] for t in queries.tags_of(ctx.conn, req_id)])


@router.post("/req/{req_id}/edit")
async def req_edit(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    req = queries.requirement_or_404(ctx.conn, req_id)
    project = str(form.get("project") or req["project"])
    name = str(form.get("name") or "").strip()
    if project not in PROJECTS or not name:
        ctx.flash("err", "请填写项目与名称")
        return ctx.redirect(f"/req/{req_id}/edit")
    db.update(
        ctx.conn, "requirements", req_id,
        {"project": project, "name": name, "jira_keys": str(form.get("jira_keys") or "").strip(), "notes": str(form.get("notes") or "").strip(),
         "updated_at": db.utcnow(), "updated_by": ctx.user["id"]},
    )
    _set_owners(ctx, req_id, _parse_owner_ids(form))
    queries.set_tags(ctx.conn, req_id, queries.parse_ids(form, "tags"))
    if req["kind"] == "single":
        doc = queries.primary_document(ctx.conn, req_id)
        if doc:
            db.update(ctx.conn, "documents", doc["id"], {"name": name, "updated_at": db.utcnow()})
    ctx.flash("ok", "已保存")
    return ctx.redirect(f"/req/{req_id}")


@router.post("/req/{req_id}/tags")
async def req_tags(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    """详情页快速挂标签（所有登录用户）。"""
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    queries.requirement_or_404(ctx.conn, req_id)
    queries.set_tags(ctx.conn, req_id, queries.parse_ids(form, "tags"))
    db.update(ctx.conn, "requirements", req_id, {"updated_at": db.utcnow(), "updated_by": ctx.user["id"]})
    ctx.flash("ok", "标签已更新")
    return ctx.redirect(f"/req/{req_id}")


@router.post("/req/{req_id}/delete")
async def req_delete(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    req = queries.requirement_or_404(ctx.conn, req_id)
    if not can_delete(ctx, req):
        raise HTTPException(403, "只能删除自己创建的需求")
    db.update(ctx.conn, "requirements", req_id, {"deleted_at": db.utcnow(), "deleted_by": ctx.user["id"]})
    db.audit(ctx.conn, ctx.user, "delete_requirement", "requirement", req_id, {"name": req["name"], "project": req["project"]})
    ctx.flash("ok", f"需求「{req['name']}」已删除")
    return ctx.redirect("/")


@router.post("/req/{req_id}/convert")
async def req_convert(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    req = queries.requirement_or_404(ctx.conn, req_id)
    if req["kind"] != "single":
        ctx.flash("err", "该需求已经是复合需求")
        return ctx.redirect(f"/req/{req_id}")
    doc = queries.primary_document(ctx.conn, req_id)
    if doc:
        # 已发出去的文档链接不变：原文档码交给目录页，文档换新码
        old_doc_code = doc["share_code"]
        db.update(ctx.conn, "documents", doc["id"], {"share_code": new_share_code(ctx.conn), "updated_at": db.utcnow()})
        db.update(ctx.conn, "requirements", req_id, {"share_code": old_doc_code})
    db.update(ctx.conn, "requirements", req_id, {"kind": "compound", "updated_at": db.utcnow(), "updated_by": ctx.user["id"]})
    ctx.flash("ok", "已转为复合需求：原来的分享链接现在打开目录页，原文档成为第一个子文档（子文档有了新的直达链接）")
    return ctx.redirect(f"/req/{req_id}")


@router.post("/req/{req_id}/reset-share")
async def req_reset_share(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    req = queries.requirement_or_404(ctx.conn, req_id)
    if req["kind"] == "single":
        doc = queries.primary_document(ctx.conn, req_id)
        if doc:
            code = new_share_code(ctx.conn)
            db.update(ctx.conn, "documents", doc["id"], {"share_code": code, "updated_at": db.utcnow()})
            db.audit(ctx.conn, ctx.user, "reset_share", "document", doc["id"], {"old": doc["share_code"], "new": code})
    else:
        code = new_share_code(ctx.conn)
        db.update(ctx.conn, "requirements", req_id, {"share_code": code})
        db.audit(ctx.conn, ctx.user, "reset_share", "requirement", req_id, {"old": req["share_code"], "new": code})
    ctx.flash("ok", "分享链接已重置，旧链接已失效")
    return ctx.redirect(f"/req/{req_id}")


@router.post("/req/{req_id}/docs/new")
async def req_doc_new(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    req = queries.requirement_or_404(ctx.conn, req_id)
    if req["kind"] != "compound":
        raise HTTPException(400, "只有复合需求可以新增子文档")
    name = str(form.get("name") or "").strip()
    if not name:
        ctx.flash("err", "子文档名称不能为空")
        return ctx.redirect(f"/req/{req_id}")
    now = db.utcnow()
    pos = db.one(ctx.conn, "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM documents WHERE requirement_id = ?", (req_id,))["p"]
    doc_id = db.insert(
        ctx.conn,
        "documents",
        {"requirement_id": req_id, "name": name, "notes": str(form.get("notes") or "").strip(), "share_code": new_share_code(ctx.conn),
         "position": pos, "created_by": ctx.user["id"], "created_at": now, "updated_at": now},
    )
    queries.touch_requirement(ctx.conn, req_id, ctx.user["id"])
    upload = form.get("file")
    if isinstance(upload, UploadFile) and upload.filename:
        doc = db.one(ctx.conn, "SELECT * FROM documents WHERE id = ?", (doc_id,))
        try:
            result = await handle_upload(ctx, doc, upload, str(form.get("note") or ""))
        except UploadError as e:
            ctx.flash("err", f"子文档已创建，但文件上传失败：{e}")
            return ctx.redirect(f"/doc/{doc_id}")
        if result and result[1]:
            ctx.flash("ok", "子文档已创建，请选择入口文件")
            return ctx.redirect(f"/ver/{result[0]}/entry")
    ctx.flash("ok", f"子文档「{name}」已创建")
    return ctx.redirect(f"/req/{req_id}")
