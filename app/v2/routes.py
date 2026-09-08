"""2.0 后台路由：菜单视图、页面查看器、需求单视图、系统/端/菜单/快照管理、批注。"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .. import db
from ..main import get_ctx
from ..queries import active_users
from ..routes.public import SANDBOX_CSP
from ..security import new_share_code
from ..storage import copy_stream_limited
from ..web import PROJECTS, Ctx
from . import queries as q
from .diff_html import inject_diff
from .snapshot import import_snapshot, normalize_menu, replace_menu
from .storage import V2Error, compute_diff, content_path, create_version, latest_baseline, media_for

router = APIRouter(prefix="/v2")
STATUS_LABELS = {"draft": "草稿", "pending": "待上线", "live": "已上线"}


def _render(ctx: Ctx, template: str, **kw):
    kw.setdefault("STATUS_LABELS", STATUS_LABELS)
    return ctx.render(f"v2/{template}", **kw)


async def _save_upload(ctx: Ctx, upload: UploadFile) -> Path:
    tmp = ctx.cfg.tmp_dir / f"v2-{uuid.uuid4().hex}"
    ctx.cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
    await run_in_threadpool(copy_stream_limited, upload.file, tmp, ctx.max_upload_bytes)
    await upload.close()
    return tmp


def _viewer_context(ctx: Ctx, page, version_id: int | None):
    versions = q.versions_of_page(ctx.conn, page["id"])
    selected = None
    if version_id:
        selected = next((v for v in versions if v["id"] == version_id), None)
    if selected is None:
        baselines = [v for v in versions if v["kind"] == "baseline"]
        selected = baselines[-1] if baselines else (versions[-1] if versions else None)
    base = None
    diff = None
    others: list = []
    if selected and selected["kind"] == "proposal":
        if selected["base_version_id"]:
            base = db.one(ctx.conn, "SELECT v.*, r.version AS release_version FROM page_versions v LEFT JOIN releases r ON r.id = v.release_id WHERE v.id = ?", (selected["base_version_id"],))
        diff = json.loads(selected["diff_json"]) if selected["diff_json"] else None
        if selected["requirement_id"]:
            others = q.other_pages_in_requirement(ctx.conn, selected["requirement_id"], page["id"])
    return {
        "page": page,
        "versions": versions,
        "selected": selected,
        "base": base,
        "diff": diff,
        "others": others,
        "annotations": q.annotations_of(ctx.conn, selected["id"]) if selected else [],
        "menu_path": q.menu_path(ctx.conn, page["id"]),
    }


# ---------- 菜单视图 / 查看器 ----------

@router.get("/")
async def menu_view(ctx: Ctx = Depends(get_ctx), app: int | None = None, project: str = ""):
    ctx.require_user()
    groups = q.systems_with_apps(ctx.conn)
    app_row = None
    if app:
        app_row = q.app_or_404(ctx.conn, app)
    elif groups:
        for g in groups:
            if g["apps"] and (not project or g["system"]["project"] == project):
                app_row = q.app_or_404(ctx.conn, g["apps"][0]["id"])
                break
    tree = q.menu_tree(ctx.conn, app_row["id"]) if app_row else []
    unlisted = q.unlisted_pages(ctx.conn, app_row["id"]) if app_row else []
    return _render(ctx, "menu.html", groups=groups, app=app_row, tree_items=tree, unlisted=unlisted, viewer=None, project=project)


@router.get("/p/{page_id}")
async def page_view(page_id: int, ctx: Ctx = Depends(get_ctx), v: int | None = None):
    ctx.require_user()
    page = q.page_or_404(ctx.conn, page_id)
    app_row = q.app_or_404(ctx.conn, page["app_id"])
    viewer = _viewer_context(ctx, page, v)
    my_reqs = db.all_rows(
        ctx.conn,
        "SELECT id, name, jira_keys FROM requirements WHERE kind = 'v2' AND deleted_at IS NULL AND v2_status != 'live' AND project = ? ORDER BY updated_at DESC LIMIT 50",
        (page["project"],),
    )
    return _render(
        ctx, "menu.html",
        groups=q.systems_with_apps(ctx.conn), app=app_row, tree_items=q.menu_tree(ctx.conn, app_row["id"]), unlisted=q.unlisted_pages(ctx.conn, app_row["id"]),
        viewer=viewer, my_reqs=my_reqs, project=page["project"],
    )


@router.post("/p/{page_id}/start")
async def page_start_change(page_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    """基于此页发起改动：加入已有需求，或跳到新建需求表单。"""
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    page = q.page_or_404(ctx.conn, page_id)
    base = form.get("base_version_id")
    base_id = int(base) if base and str(base).isdigit() else None
    req = str(form.get("req_id") or "")
    if req == "new" or not req:
        return ctx.redirect(f"/v2/req/new?page_id={page_id}&project={page['project']}" + (f"&base_version_id={base_id}" if base_id else ""))
    q.requirement_v2_or_404(ctx.conn, int(req))
    _add_page(ctx, int(req), page_id, base_id)
    ctx.flash("ok", "页面已加入需求，请下载基线修改后上传改稿")
    return ctx.redirect(f"/v2/req/{req}")


def _serve_version(ctx: Ctx, ver, diff: bool) -> Response:
    path = content_path(ctx.cfg, ver)
    if not path.is_file():
        raise HTTPException(404, "版本内容不存在")
    if ver["media"] == "image":
        resp = FileResponse(str(path))
        resp.headers["cache-control"] = "private, max-age=3600"
        return resp
    html = path.read_text("utf-8", errors="replace")
    changes = []
    if diff and ver["diff_json"]:
        changes = json.loads(ver["diff_json"]).get("changes", [])
    html = inject_diff(html, changes)
    resp = Response(html, media_type="text/html")
    resp.headers["content-type"] = "text/html; charset=utf-8"
    resp.headers["content-security-policy"] = SANDBOX_CSP if ctx.sandbox_enabled else "sandbox allow-scripts allow-same-origin allow-forms allow-popups allow-modals"
    resp.headers["cache-control"] = "no-store"
    return resp


@router.get("/content/{version_id}")
async def version_content(version_id: int, ctx: Ctx = Depends(get_ctx), diff: int = 0):
    ctx.require_user()
    return _serve_version(ctx, q.version_or_404(ctx.conn, version_id), bool(diff))


@router.get("/download/{version_id}")
async def version_download(version_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    ver = q.version_or_404(ctx.conn, version_id)
    page = q.page_or_404(ctx.conn, ver["page_id"])
    path = content_path(ctx.cfg, ver)
    if not path.is_file():
        raise HTTPException(404, "版本内容不存在")
    label = f"{page['title']}-{'基线' + (ver['release_version'] or '') if ver['kind'] == 'baseline' else '改稿'}{path.suffix}"
    return FileResponse(str(path), filename=label, media_type="application/octet-stream")


@router.post("/version/{version_id}/delete")
async def version_delete(version_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    if not ctx.is_admin:
        raise HTTPException(403, "只有管理员可以删除版本")
    ver = q.version_or_404(ctx.conn, version_id)
    db.update(ctx.conn, "page_versions", version_id, {"deleted_at": db.utcnow(), "deleted_by": ctx.user["id"]})
    db.audit(ctx.conn, ctx.user, "v2_delete_version", "page_version", version_id, {"page_id": ver["page_id"], "kind": ver["kind"]})
    ctx.flash("ok", "版本已删除")
    return ctx.redirect(f"/v2/p/{ver['page_id']}")


# ---------- 批注 ----------

@router.get("/annotations/{version_id}")
async def annotations_list(version_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    q.version_or_404(ctx.conn, version_id)
    return JSONResponse([dict(a) for a in q.annotations_of(ctx.conn, version_id)])


@router.post("/annotations/{version_id}")
async def annotations_create(version_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    data = await request.json()
    ctx.check_csrf({"csrf": request.headers.get("x-csrf", "")})
    q.version_or_404(ctx.conn, version_id)
    try:
        x, y, w, h = (max(0.0, min(1.0, float(data[k]))) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "坐标不合法")
    if w <= 0 or h <= 0:
        raise HTTPException(400, "坐标不合法")
    aid = db.insert(ctx.conn, "annotations", {"version_id": version_id, "x": x, "y": y, "w": w, "h": h, "text": str(data.get("text") or "")[:500], "created_by": ctx.user["id"], "created_at": db.utcnow()})
    return JSONResponse({"id": aid, "author": ctx.user["name"]})


@router.post("/annotations/{version_id}/{ann_id}/delete")
async def annotations_delete(version_id: int, ann_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    ctx.check_csrf({"csrf": request.headers.get("x-csrf", "")})
    ann = db.one(ctx.conn, "SELECT * FROM annotations WHERE id = ? AND version_id = ? AND deleted_at IS NULL", (ann_id, version_id))
    if not ann:
        raise HTTPException(404, "批注不存在")
    if ann["created_by"] != ctx.user["id"] and not ctx.is_admin:
        raise HTTPException(403, "只能删除自己的批注")
    db.update(ctx.conn, "annotations", ann_id, {"deleted_at": db.utcnow()})
    return JSONResponse({"ok": True})


# ---------- 需求单视图 ----------

def _can_delete_req(ctx: Ctx, req) -> bool:
    return ctx.is_admin or req["created_by"] == ctx.user["id"]


@router.get("/req")
async def req_list(ctx: Ctx = Depends(get_ctx), project: str = "", status: str = "", q_: str = "", q: str = ""):
    ctx.require_user()
    kw = (q or q_).strip()
    where = ["r.kind = 'v2'", "r.deleted_at IS NULL"]
    params: list = []
    if project in PROJECTS:
        where.append("r.project = ?"); params.append(project)
    if status in STATUS_LABELS:
        where.append("r.v2_status = ?"); params.append(status)
    if kw:
        like = f"%{kw}%"
        where.append(
            """(r.name LIKE ? OR r.jira_keys LIKE ?
                OR EXISTS (SELECT 1 FROM requirement_owners ro JOIN users u ON u.id = ro.user_id WHERE ro.requirement_id = r.id AND u.name LIKE ?)
                OR EXISTS (SELECT 1 FROM requirement_pages rp JOIN pages p ON p.id = rp.page_id WHERE rp.requirement_id = r.id AND rp.deleted_at IS NULL AND (p.title LIKE ? OR p.route_key LIKE ?)))"""
        )
        params += [like] * 5
    rows = db.all_rows(
        ctx.conn,
        f"""SELECT r.*, (SELECT GROUP_CONCAT(u.name, '、') FROM requirement_owners ro JOIN users u ON u.id = ro.user_id WHERE ro.requirement_id = r.id) AS owner_names,
              (SELECT COUNT(*) FROM requirement_pages rp WHERE rp.requirement_id = r.id AND rp.deleted_at IS NULL) AS page_count,
              (SELECT GROUP_CONCAT(a.name || CASE WHEN ra.release_version != '' THEN ' ' || ra.release_version ELSE '' END, '，') FROM requirement_apps ra JOIN apps a ON a.id = ra.app_id WHERE ra.requirement_id = r.id) AS app_versions
            FROM requirements r WHERE {' AND '.join(where)} ORDER BY r.updated_at DESC, r.id DESC LIMIT 200""",
        params,
    )
    return _render(ctx, "req_list.html", reqs=rows, project=project, status=status, q=kw)


@router.get("/req/new")
async def req_new_page(ctx: Ctx = Depends(get_ctx), project: str = "", page_id: int | None = None, base_version_id: int | None = None):
    ctx.require_user()
    page = q.page_or_404(ctx.conn, page_id) if page_id else None
    return _render(ctx, "req_form.html", req=None, owners=[], users=active_users(ctx.conn), mode="new", project=project or (page["project"] if page else ""), page=page, base_version_id=base_version_id)


def _owner_ids(form) -> list[int]:
    out = []
    for v in form.getlist("owners"):
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            pass
    return out


def _set_owners(ctx: Ctx, req_id: int, ids: list[int]) -> None:
    ctx.conn.execute("DELETE FROM requirement_owners WHERE requirement_id = ?", (req_id,))
    for uid in ids:
        ctx.conn.execute("INSERT OR IGNORE INTO requirement_owners(requirement_id, user_id) VALUES (?, ?)", (req_id, uid))


@router.post("/req/new")
async def req_new(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    project = str(form.get("project") or "")
    name = str(form.get("name") or "").strip()
    if project not in PROJECTS or not name:
        ctx.flash("err", "请填写项目与名称")
        return ctx.redirect("/v2/req/new")
    now = db.utcnow()
    rid = db.insert(
        ctx.conn, "requirements",
        {"project": project, "kind": "v2", "name": name, "jira_keys": str(form.get("jira_keys") or "").strip(), "notes": str(form.get("notes") or "").strip(),
         "share_code": new_share_code(ctx.conn), "created_by": ctx.user["id"], "created_at": now, "updated_by": ctx.user["id"], "updated_at": now, "v2_status": "draft"},
    )
    _set_owners(ctx, rid, _owner_ids(form))
    page_id = form.get("page_id")
    if page_id and str(page_id).isdigit():
        base = form.get("base_version_id")
        _add_page(ctx, rid, int(page_id), int(base) if base and str(base).isdigit() else None)
    ctx.flash("ok", "需求已创建")
    return ctx.redirect(f"/v2/req/{rid}")


@router.get("/req/{req_id}")
async def req_detail(req_id: int, ctx: Ctx = Depends(get_ctx), pq: str = ""):
    ctx.require_user()
    req = q.requirement_v2_or_404(ctx.conn, req_id)
    results = q.search_pages(ctx.conn, pq.strip(), req["project"]) if pq.strip() else []
    apps = db.all_rows(ctx.conn, "SELECT a.*, s.name AS system_name FROM apps a JOIN systems s ON s.id = a.system_id WHERE a.deleted_at IS NULL AND s.deleted_at IS NULL AND s.project = ? ORDER BY s.position, a.position", (req["project"],))
    owners = db.all_rows(ctx.conn, "SELECT u.* FROM requirement_owners ro JOIN users u ON u.id = ro.user_id WHERE ro.requirement_id = ? ORDER BY u.name", (req_id,))
    return _render(
        ctx, "req_detail.html", req=req, pages=q.requirement_pages(ctx.conn, req_id), req_apps=q.requirement_apps(ctx.conn, req_id), apps=apps,
        owners=owners, results=results, pq=pq, can_delete=_can_delete_req(ctx, req), creator=db.one(ctx.conn, "SELECT name FROM users WHERE id = ?", (req["created_by"],)),
    )


@router.get("/req/{req_id}/edit")
async def req_edit_page(req_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    req = q.requirement_v2_or_404(ctx.conn, req_id)
    owners = [r["user_id"] for r in db.all_rows(ctx.conn, "SELECT user_id FROM requirement_owners WHERE requirement_id = ?", (req_id,))]
    return _render(ctx, "req_form.html", req=req, owners=owners, users=active_users(ctx.conn), mode="edit", project=req["project"])


@router.post("/req/{req_id}/edit")
async def req_edit(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    req = q.requirement_v2_or_404(ctx.conn, req_id)
    name = str(form.get("name") or "").strip()
    if not name:
        ctx.flash("err", "名称不能为空")
        return ctx.redirect(f"/v2/req/{req_id}/edit")
    db.update(ctx.conn, "requirements", req_id, {"name": name, "jira_keys": str(form.get("jira_keys") or "").strip(), "notes": str(form.get("notes") or "").strip(), "updated_at": db.utcnow(), "updated_by": ctx.user["id"]})
    _set_owners(ctx, req_id, _owner_ids(form))
    ctx.flash("ok", "已保存")
    return ctx.redirect(f"/v2/req/{req_id}")


@router.post("/req/{req_id}/status")
async def req_status(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    q.requirement_v2_or_404(ctx.conn, req_id)
    status = str(form.get("status") or "")
    if status not in STATUS_LABELS:
        raise HTTPException(400, "状态不合法")
    db.update(ctx.conn, "requirements", req_id, {"v2_status": status, "updated_at": db.utcnow(), "updated_by": ctx.user["id"]})
    if status == "live":
        ctx.conn.execute("UPDATE requirement_apps SET live_at = COALESCE(live_at, ?) WHERE requirement_id = ?", (db.utcnow(), req_id))
    ctx.flash("ok", f"状态已改为「{STATUS_LABELS[status]}」")
    return ctx.redirect(f"/v2/req/{req_id}")


@router.post("/req/{req_id}/apps")
async def req_apps_save(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    q.requirement_v2_or_404(ctx.conn, req_id)
    for key, value in form.multi_items():
        if key.startswith("release_") and key[8:].isdigit():
            ctx.conn.execute(
                "INSERT INTO requirement_apps(requirement_id, app_id, release_version) VALUES (?, ?, ?) ON CONFLICT(requirement_id, app_id) DO UPDATE SET release_version = excluded.release_version",
                (req_id, int(key[8:]), str(value).strip()),
            )
    db.update(ctx.conn, "requirements", req_id, {"updated_at": db.utcnow(), "updated_by": ctx.user["id"]})
    ctx.flash("ok", "上线版本已保存；导入对应 release 快照时会自动标记为已上线")
    return ctx.redirect(f"/v2/req/{req_id}")


@router.post("/req/{req_id}/delete")
async def req_delete(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    req = q.requirement_v2_or_404(ctx.conn, req_id)
    if not _can_delete_req(ctx, req):
        raise HTTPException(403, "只能删除自己创建的需求")
    db.update(ctx.conn, "requirements", req_id, {"deleted_at": db.utcnow(), "deleted_by": ctx.user["id"]})
    db.audit(ctx.conn, ctx.user, "v2_delete_requirement", "requirement", req_id, {"name": req["name"]})
    ctx.flash("ok", f"需求「{req['name']}」已删除")
    return ctx.redirect("/v2/req")


@router.post("/req/{req_id}/reset-share")
async def req_reset_share(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    req = q.requirement_v2_or_404(ctx.conn, req_id)
    code = new_share_code(ctx.conn)
    db.update(ctx.conn, "requirements", req_id, {"share_code": code})
    db.audit(ctx.conn, ctx.user, "reset_share", "requirement", req_id, {"old": req["share_code"], "new": code})
    ctx.flash("ok", "分享链接已重置")
    return ctx.redirect(f"/v2/req/{req_id}")


def _add_page(ctx: Ctx, req_id: int, page_id: int, base_version_id: int | None = None) -> None:
    page = q.page_or_404(ctx.conn, page_id)
    if base_version_id is None:
        base = latest_baseline(ctx.conn, page_id)
        base_version_id = base["id"] if base else None
    existing = db.one(ctx.conn, "SELECT * FROM requirement_pages WHERE requirement_id = ? AND page_id = ?", (req_id, page_id))
    if existing:
        ctx.conn.execute("UPDATE requirement_pages SET deleted_at = NULL, base_version_id = COALESCE(base_version_id, ?) WHERE requirement_id = ? AND page_id = ?", (base_version_id, req_id, page_id))
    else:
        db.insert(ctx.conn, "requirement_pages", {"requirement_id": req_id, "page_id": page_id, "base_version_id": base_version_id, "added_by": ctx.user["id"], "added_at": db.utcnow()})
    q.sync_requirement_apps(ctx.conn, req_id)
    db.update(ctx.conn, "requirements", req_id, {"updated_at": db.utcnow(), "updated_by": ctx.user["id"]})


@router.post("/req/{req_id}/pages/add")
async def req_page_add(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    q.requirement_v2_or_404(ctx.conn, req_id)
    page_id = int(str(form.get("page_id") or 0))
    base = form.get("base_version_id")
    _add_page(ctx, req_id, page_id, int(base) if base and str(base).isdigit() else None)
    ctx.flash("ok", "页面已加入需求，请下载基线修改后上传改稿")
    return ctx.redirect(f"/v2/req/{req_id}")


@router.post("/req/{req_id}/pages/new")
async def req_page_new(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    q.requirement_v2_or_404(ctx.conn, req_id)
    app_id = int(str(form.get("app_id") or 0))
    app_row = q.app_or_404(ctx.conn, app_id)
    title = str(form.get("title") or "").strip()
    route = str(form.get("route_key") or "").strip()
    if not title or not route:
        ctx.flash("err", "新增页面需要填写标题与路由")
        return ctx.redirect(f"/v2/req/{req_id}")
    if db.one(ctx.conn, "SELECT 1 FROM pages WHERE app_id = ? AND route_key = ? AND deleted_at IS NULL", (app_id, route)):
        ctx.flash("err", "该路由已存在，请直接搜索添加")
        return ctx.redirect(f"/v2/req/{req_id}")
    page_id = db.insert(ctx.conn, "pages", {"app_id": app_id, "route_key": route, "title": title, "is_new": 1, "created_by": ctx.user["id"], "created_at": db.utcnow()})
    parent = form.get("parent_id")
    parent_id = int(parent) if parent and str(parent).isdigit() else None
    pos = db.one(ctx.conn, "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM menu_nodes WHERE app_id = ? AND parent_id IS ? AND deleted_at IS NULL", (app_id, parent_id))["p"]
    db.insert(ctx.conn, "menu_nodes", {"app_id": app_row["id"], "parent_id": parent_id, "title": title, "route_key": route, "page_id": page_id, "position": pos, "created_at": db.utcnow()})
    _add_page(ctx, req_id, page_id, None)
    ctx.flash("ok", f"新增页面「{title}」已挂到菜单并加入需求")
    return ctx.redirect(f"/v2/req/{req_id}")


@router.post("/req/{req_id}/pages/{page_id}/remove")
async def req_page_remove(req_id: int, page_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    q.requirement_v2_or_404(ctx.conn, req_id)
    now = db.utcnow()
    ctx.conn.execute("UPDATE requirement_pages SET deleted_at = ? WHERE requirement_id = ? AND page_id = ?", (now, req_id, page_id))
    ctx.conn.execute("UPDATE page_versions SET deleted_at = ?, deleted_by = ? WHERE requirement_id = ? AND page_id = ? AND deleted_at IS NULL", (now, ctx.user["id"], req_id, page_id))
    page = db.one(ctx.conn, "SELECT * FROM pages WHERE id = ?", (page_id,))
    if page and page["is_new"] and not db.one(ctx.conn, "SELECT 1 FROM requirement_pages WHERE page_id = ? AND deleted_at IS NULL", (page_id,)):
        db.update(ctx.conn, "pages", page_id, {"deleted_at": now})
        ctx.conn.execute("UPDATE menu_nodes SET deleted_at = ? WHERE page_id = ? AND deleted_at IS NULL", (now, page_id))
    db.audit(ctx.conn, ctx.user, "v2_remove_page", "requirement", req_id, {"page_id": page_id})
    ctx.flash("ok", "已从需求中移除该页面")
    return ctx.redirect(f"/v2/req/{req_id}")


@router.post("/req/{req_id}/pages/{page_id}/upload")
async def req_page_upload(req_id: int, page_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    q.requirement_v2_or_404(ctx.conn, req_id)
    page = q.page_or_404(ctx.conn, page_id)
    rp = db.one(ctx.conn, "SELECT * FROM requirement_pages WHERE requirement_id = ? AND page_id = ? AND deleted_at IS NULL", (req_id, page_id))
    if not rp:
        raise HTTPException(400, "该页面不在此需求中")
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        ctx.flash("err", "请选择文件")
        return ctx.redirect(f"/v2/req/{req_id}")
    try:
        expected = page["renderer"] if page["renderer"] in ("html", "image") else None
        media = media_for(upload.filename, expected)
        tmp = await _save_upload(ctx, upload)
        try:
            vid = create_version(ctx.cfg, ctx.conn, page_id, "proposal", media, tmp, upload.filename, ctx.user["id"], requirement_id=req_id, base_version_id=rp["base_version_id"], note=str(form.get("note") or "").strip())
        finally:
            tmp.unlink(missing_ok=True)
        await run_in_threadpool(compute_diff, ctx.cfg, ctx.conn, vid)
    except V2Error as e:
        ctx.flash("err", str(e))
        return ctx.redirect(f"/v2/req/{req_id}")
    except Exception as e:  # noqa: BLE001
        ctx.flash("err", f"上传失败：{e}")
        return ctx.redirect(f"/v2/req/{req_id}")
    if req_status_row := db.one(ctx.conn, "SELECT v2_status FROM requirements WHERE id = ?", (req_id,)):
        if req_status_row["v2_status"] == "draft":
            pass
    db.update(ctx.conn, "requirements", req_id, {"updated_at": db.utcnow(), "updated_by": ctx.user["id"]})
    ctx.flash("ok", "改稿已上传并完成自动对比")
    return ctx.redirect(f"/v2/p/{page_id}?v={vid}")


# ---------- 系统 / 端 / 菜单 / 快照 管理 ----------

@router.get("/admin")
async def admin_home(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    return _render(ctx, "admin.html", groups=q.systems_with_apps(ctx.conn))


@router.post("/admin/systems/new")
async def system_new(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    project, name = str(form.get("project") or ""), str(form.get("name") or "").strip()
    if project not in PROJECTS or not name:
        ctx.flash("err", "请填写项目与系统名称")
        return ctx.redirect("/v2/admin")
    db.insert(ctx.conn, "systems", {"project": project, "name": name, "notes": str(form.get("notes") or ""), "created_by": ctx.user["id"], "created_at": db.utcnow()})
    ctx.flash("ok", f"系统「{name}」已创建")
    return ctx.redirect("/v2/admin")


@router.post("/admin/systems/{sid}/edit")
async def system_edit(sid: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    name = str(form.get("name") or "").strip()
    if name:
        db.update(ctx.conn, "systems", sid, {"name": name, "notes": str(form.get("notes") or "")})
    ctx.flash("ok", "已保存")
    return ctx.redirect("/v2/admin")


@router.post("/admin/systems/{sid}/delete")
async def system_delete(sid: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    if db.one(ctx.conn, "SELECT 1 FROM apps WHERE system_id = ? AND deleted_at IS NULL", (sid,)):
        ctx.flash("err", "请先删除该系统下的端")
        return ctx.redirect("/v2/admin")
    db.update(ctx.conn, "systems", sid, {"deleted_at": db.utcnow()})
    db.audit(ctx.conn, ctx.user, "v2_delete_system", "system", sid)
    ctx.flash("ok", "系统已删除")
    return ctx.redirect("/v2/admin")


@router.post("/admin/apps/new")
async def app_new(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    system_id = int(str(form.get("system_id") or 0))
    name, key = str(form.get("name") or "").strip(), str(form.get("key") or "").strip()
    if not name or not key or not db.one(ctx.conn, "SELECT 1 FROM systems WHERE id = ? AND deleted_at IS NULL", (system_id,)):
        ctx.flash("err", "请填写端名称与标识")
        return ctx.redirect("/v2/admin")
    if db.one(ctx.conn, "SELECT 1 FROM apps WHERE key = ?", (key,)):
        ctx.flash("err", f"标识 {key} 已被使用")
        return ctx.redirect("/v2/admin")
    kind = str(form.get("kind") or "web")
    renderer = "image" if kind == "flutter_web" or form.get("renderer") == "image" else "html"
    aid = db.insert(ctx.conn, "apps", {"system_id": system_id, "name": name, "key": key, "kind": kind, "renderer": renderer, "base_url": str(form.get("base_url") or "").strip(), "notes": "", "created_at": db.utcnow()})
    ctx.flash("ok", f"端「{name}」已创建")
    return ctx.redirect(f"/v2/admin/apps/{aid}")


@router.get("/admin/apps/{app_id}")
async def app_detail(app_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    app_row = q.app_or_404(ctx.conn, app_id)
    releases = db.all_rows(ctx.conn, "SELECT r.*, u.name AS importer FROM releases r LEFT JOIN users u ON u.id = r.imported_by WHERE r.app_id = ? ORDER BY r.id DESC", (app_id,))
    imports = db.all_rows(ctx.conn, "SELECT i.*, r.version, u.name AS importer FROM snapshot_imports i JOIN releases r ON r.id = i.release_id LEFT JOIN users u ON u.id = i.imported_by WHERE i.app_id = ? ORDER BY i.id DESC LIMIT 30", (app_id,))
    return _render(ctx, "admin_app.html", app=app_row, tree_items=q.menu_tree(ctx.conn, app_id), unlisted=q.unlisted_pages(ctx.conn, app_id), releases=releases, imports=imports)


@router.post("/admin/apps/{app_id}/edit")
async def app_edit(app_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    q.app_or_404(ctx.conn, app_id)
    kind = str(form.get("kind") or "web")
    db.update(ctx.conn, "apps", app_id, {"name": str(form.get("name") or "").strip() or "未命名", "kind": kind, "renderer": "image" if form.get("renderer") == "image" else "html", "base_url": str(form.get("base_url") or "").strip(), "notes": str(form.get("notes") or "")})
    ctx.flash("ok", "已保存")
    return ctx.redirect(f"/v2/admin/apps/{app_id}")


@router.post("/admin/apps/{app_id}/delete")
async def app_delete(app_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    q.app_or_404(ctx.conn, app_id)
    db.update(ctx.conn, "apps", app_id, {"deleted_at": db.utcnow()})
    db.audit(ctx.conn, ctx.user, "v2_delete_app", "app", app_id)
    ctx.flash("ok", "端已删除（页面与版本数据保留）")
    return ctx.redirect("/v2/admin")


@router.post("/admin/apps/{app_id}/menu/import")
async def menu_import(app_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    q.app_or_404(ctx.conn, app_id)
    raw = str(form.get("menu_json") or "")
    upload = form.get("file")
    if isinstance(upload, UploadFile) and upload.filename:
        raw = (await upload.read()).decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        ctx.flash("err", f"JSON 解析失败：{e}")
        return ctx.redirect(f"/v2/admin/apps/{app_id}")
    menu = normalize_menu(data)
    if not menu:
        ctx.flash("err", "没有识别出任何菜单项（需要 title/path 或 name/route 等字段）")
        return ctx.redirect(f"/v2/admin/apps/{app_id}")
    n = replace_menu(ctx.conn, app_id, menu, ctx.user["id"])
    db.audit(ctx.conn, ctx.user, "v2_import_menu", "app", app_id, {"nodes": n})
    ctx.flash("ok", f"菜单已导入，共 {n} 个节点")
    return ctx.redirect(f"/v2/admin/apps/{app_id}")


@router.post("/admin/apps/{app_id}/menu/node")
async def menu_node_new(app_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    q.app_or_404(ctx.conn, app_id)
    title, route = str(form.get("title") or "").strip(), str(form.get("route_key") or "").strip()
    parent = form.get("parent_id")
    parent_id = int(parent) if parent and str(parent).isdigit() else None
    if not title:
        ctx.flash("err", "请填写标题")
        return ctx.redirect(f"/v2/admin/apps/{app_id}")
    page_id = None
    if route:
        from .snapshot import _get_or_create_page
        page_id = _get_or_create_page(ctx.conn, app_id, route, title, ctx.user["id"])
    pos = db.one(ctx.conn, "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM menu_nodes WHERE app_id = ? AND parent_id IS ? AND deleted_at IS NULL", (app_id, parent_id))["p"]
    db.insert(ctx.conn, "menu_nodes", {"app_id": app_id, "parent_id": parent_id, "title": title, "route_key": route or None, "page_id": page_id, "position": pos, "created_at": db.utcnow()})
    ctx.flash("ok", "菜单节点已添加")
    return ctx.redirect(f"/v2/admin/apps/{app_id}")


@router.post("/admin/apps/{app_id}/menu/node/{node_id}/delete")
async def menu_node_delete(app_id: int, node_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    now = db.utcnow()
    ids = [node_id]
    while ids:
        nid = ids.pop()
        ctx.conn.execute("UPDATE menu_nodes SET deleted_at = ? WHERE id = ? AND app_id = ?", (now, nid, app_id))
        ids += [r["id"] for r in db.all_rows(ctx.conn, "SELECT id FROM menu_nodes WHERE parent_id = ? AND deleted_at IS NULL", (nid,))]
    ctx.flash("ok", "菜单节点已删除（页面数据保留，显示在“未挂菜单的页面”）")
    return ctx.redirect(f"/v2/admin/apps/{app_id}")


@router.post("/admin/apps/{app_id}/snapshots/import")
async def snapshot_import(app_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    form = await request.form()
    ctx.check_csrf(form)
    app_row = q.app_or_404(ctx.conn, app_id)
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        ctx.flash("err", "请选择快照 zip")
        return ctx.redirect(f"/v2/admin/apps/{app_id}")
    try:
        tmp = await _save_upload(ctx, upload)
        try:
            result = await run_in_threadpool(import_snapshot, ctx.cfg, ctx.conn, app_row, tmp, upload.filename, ctx.user["id"], str(form.get("release") or ""))
        finally:
            tmp.unlink(missing_ok=True)
    except V2Error as e:
        ctx.flash("err", str(e))
        return ctx.redirect(f"/v2/admin/apps/{app_id}")
    db.audit(ctx.conn, ctx.user, "v2_import_snapshot", "app", app_id, result)
    msg = f"快照 {result['release']} 导入完成：{result['pages']} 页，{result['new_versions']} 个新基线，{result['unchanged']} 页未变化"
    if result["aligned"]:
        msg += f"，{result['aligned']} 个需求-端标记为已上线"
    if result["errors"]:
        msg += f"；{len(result['errors'])} 条错误：{result['errors'][0]}"
    ctx.flash("ok" if not result["errors"] else "err", msg)
    return ctx.redirect(f"/v2/admin/apps/{app_id}")
