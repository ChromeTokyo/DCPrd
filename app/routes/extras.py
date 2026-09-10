"""1.0 附加功能：收藏 / 最近访问、导出 zip、上传前预览、通知测试、公开留言的后台处理。"""
from __future__ import annotations

import html as html_mod
import io
import json
import re
import tempfile
import uuid
import zipfile
from pathlib import Path

import markdown
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .. import db, queries
from ..main import get_ctx
from ..storage import UploadError, copy_stream_limited, decide_entry, detect_kind, extract_zip, sanitize_filename, version_dir
from ..web import PROJECTS, Ctx

router = APIRouter()
RECENT_KEEP = 20


# ---------- 收藏 / 最近访问 ----------

def record_view(ctx: Ctx, req_id: int) -> None:
    ctx.conn.execute(
        "INSERT INTO recent_views(user_id, requirement_id, viewed_at) VALUES (?, ?, ?) ON CONFLICT(user_id, requirement_id) DO UPDATE SET viewed_at = excluded.viewed_at",
        (ctx.user["id"], req_id, db.utcnow()),
    )
    ctx.conn.execute(
        "DELETE FROM recent_views WHERE user_id = ? AND requirement_id NOT IN (SELECT requirement_id FROM recent_views WHERE user_id = ? ORDER BY viewed_at DESC LIMIT ?)",
        (ctx.user["id"], ctx.user["id"], RECENT_KEEP),
    )


def is_fav(ctx: Ctx, req_id: int) -> bool:
    return db.one(ctx.conn, "SELECT 1 FROM favorites WHERE user_id = ? AND requirement_id = ?", (ctx.user["id"], req_id)) is not None


def fav_ids(ctx: Ctx) -> set[int]:
    return {r["requirement_id"] for r in db.all_rows(ctx.conn, "SELECT requirement_id FROM favorites WHERE user_id = ?", (ctx.user["id"],))}


def favorites_list(ctx: Ctx) -> list:
    return db.all_rows(
        ctx.conn,
        """SELECT r.id, r.name, r.project, r.kind FROM favorites f JOIN requirements r ON r.id = f.requirement_id
           WHERE f.user_id = ? AND r.deleted_at IS NULL ORDER BY f.created_at DESC""",
        (ctx.user["id"],),
    )


def recent_list(ctx: Ctx, limit: int = 8) -> list:
    return db.all_rows(
        ctx.conn,
        """SELECT r.id, r.name, r.project, r.kind, v.viewed_at FROM recent_views v JOIN requirements r ON r.id = v.requirement_id
           WHERE v.user_id = ? AND r.deleted_at IS NULL ORDER BY v.viewed_at DESC LIMIT ?""",
        (ctx.user["id"], limit),
    )


@router.post("/req/{req_id}/fav")
async def toggle_fav(req_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    queries.requirement_or_404(ctx.conn, req_id)
    if is_fav(ctx, req_id):
        ctx.conn.execute("DELETE FROM favorites WHERE user_id = ? AND requirement_id = ?", (ctx.user["id"], req_id))
        ctx.flash("ok", "已取消收藏")
    else:
        ctx.conn.execute("INSERT INTO favorites(user_id, requirement_id, created_at) VALUES (?, ?, ?)", (ctx.user["id"], req_id, db.utcnow()))
        ctx.flash("ok", "已收藏")
    back = str(form.get("back") or f"/req/{req_id}")
    return ctx.redirect(back if back.startswith("/") and not back.startswith("//") else f"/req/{req_id}")


# ---------- 导出 ----------

def _safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s or "").strip(" .")
    return s[:80] or "unnamed"


def _build_export(ctx: Ctx, req, out_path: Path) -> None:
    owners = "、".join(o["name"] for o in queries.owners_of(ctx.conn, req["id"])) or "—"
    tags = "、".join(t["name"] for t in queries.tags_of(ctx.conn, req["id"])) or "—"
    docs = db.all_rows(ctx.conn, "SELECT * FROM documents WHERE requirement_id = ? AND deleted_at IS NULL ORDER BY position, id", (req["id"],))
    lines = [
        f"# {req['name']}", "",
        f"- 项目：{PROJECTS.get(req['project'], req['project'])}",
        f"- 类型：{'复合需求' if req['kind'] == 'compound' else '单体需求'}",
        f"- Jira：{req['jira_keys'] or '—'}",
        f"- 负责人：{owners}",
        f"- 标签：{tags}",
        f"- 创建：{queries.user_name(ctx.conn, req['created_by'])} · {ctx.fmt_dt(req['created_at'])}",
        f"- 最近更新：{queries.user_name(ctx.conn, req['updated_by'])} · {ctx.fmt_dt(req['updated_at'])}",
        f"- 分享链接：{ctx.base_url}/s/{req['share_code']}/" if req["kind"] == "compound" else "",
        "", "## 备注", "", req["notes"] or "—", "", "## 文档", "",
    ]
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in docs:
            folder = _safe_name(d["name"]) if req["kind"] == "compound" else "文档"
            versions = queries.versions_of(ctx.conn, d["id"])
            lines.append(f"### {d['name']}")
            lines.append(f"- 分享链接：{ctx.base_url}/s/{d['share_code']}/")
            if d["notes"]:
                lines.append(f"- 备注：{d['notes']}")
            lines.append("")
            lines.append("| 版本 | 上传人 | 时间 | 文件 | 说明 |")
            lines.append("|---|---|---|---|---|")
            for v in versions:
                src = version_dir(ctx.cfg, d["id"], v["number"]) / "original" / v["original_filename"]
                arc = f"{folder}/v{v['number']}-{v['original_filename']}"
                if src.is_file():
                    zf.write(src, arc)
                lines.append(f"| v{v['number']} | {v['uploader_name'] or '—'} | {ctx.fmt_dt(v['uploaded_at'])} | {arc if src.is_file() else '(文件缺失)'} | {(v['note'] or '').replace('|', '/')} |")
            lines.append("")
        comments = db.all_rows(
            ctx.conn,
            """SELECT c.*, d.name AS doc_name FROM comments c JOIN documents d ON d.id = c.document_id
               WHERE d.requirement_id = ? AND c.deleted_at IS NULL ORDER BY c.id""",
            (req["id"],),
        )
        if comments:
            lines += ["## 留言", ""]
            for c in comments:
                lines.append(f"- [{c['doc_name']} v{c['version_number'] or '?'}] {c['author']} · {ctx.fmt_dt(c['created_at'])}{'（已处理）' if c['resolved_at'] else ''}：{c['body']}")
        zf.writestr("README.md", "\n".join(l for l in lines if l is not None))


@router.get("/req/{req_id}/export")
async def export_requirement(req_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    req = queries.requirement_or_404(ctx.conn, req_id)
    ctx.cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
    out = ctx.cfg.tmp_dir / f"export-{req_id}-{uuid.uuid4().hex}.zip"
    await run_in_threadpool(_build_export, ctx, req, out)
    filename = f"{_safe_name(req['name'])}.zip"
    return FileResponse(str(out), filename=filename, media_type="application/zip", background=BackgroundTask(lambda: out.unlink(missing_ok=True)))


# ---------- 上传前预览 ----------

@router.post("/preview")
async def preview_upload(request: Request, ctx: Ctx = Depends(get_ctx)):
    """不落库地渲染一个待上传文件，返回可放进 iframe srcdoc 的 HTML。"""
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        raise HTTPException(400, "没有文件")
    filename = sanitize_filename(upload.filename)
    try:
        kind = detect_kind(filename)
    except UploadError as e:
        return HTMLResponse(_msg_page(str(e)), status_code=200)
    ctx.cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = ctx.cfg.tmp_dir / f"preview-{uuid.uuid4().hex}"
    try:
        size = await run_in_threadpool(copy_stream_limited, upload.file, tmp, ctx.max_upload_bytes)
        if kind == "html":
            return HTMLResponse(tmp.read_bytes(), headers={"content-security-policy": "sandbox allow-scripts allow-popups allow-forms"})
        if kind == "md":
            body = markdown.markdown(tmp.read_text("utf-8", errors="replace"), extensions=["extra", "tables", "fenced_code", "sane_lists", "toc", "nl2br"], output_format="html5")
            return HTMLResponse(f'<!DOCTYPE html><html><head><meta charset="utf-8"><link rel="stylesheet" href="{ctx.base_url}/static/viewer.css"></head><body class="view-md"><main><article class="md">{body}</article></main></body></html>')
        if kind == "image":
            import base64
            import mimetypes
            mime = mimetypes.guess_type(filename)[0] or "image/png"
            data = base64.b64encode(tmp.read_bytes()).decode()
            return HTMLResponse(f'<!DOCTYPE html><html><head><meta charset="utf-8"><link rel="stylesheet" href="{ctx.base_url}/static/viewer.css"></head><body class="view-image"><main><figure class="pic"><img src="data:{mime};base64,{data}" alt=""></figure></main></body></html>')
        # zip：解到临时目录，给出文件清单与入口判定
        with tempfile.TemporaryDirectory(dir=ctx.cfg.tmp_dir) as d:
            try:
                res = await run_in_threadpool(extract_zip, tmp, Path(d) / "content", ctx.max_upload_bytes * 4)
            except UploadError as e:
                return HTMLResponse(_msg_page(f"压缩包无法处理：{e}"))
            entry = decide_entry(res.html_paths)
            files = sorted(str(p.relative_to(Path(d) / "content")) for p in (Path(d) / "content").rglob("*") if p.is_file())
            items = "".join(f"<li{' class=entry' if f == entry else ''}>{html_mod.escape(f)}{' ← 入口' if f == entry else ''}</li>" for f in files[:500])
            more = f"<p class='muted'>仅显示前 500 个，共 {len(files)} 个文件</p>" if len(files) > 500 else ""
            verdict = f"入口文件：<code>{html_mod.escape(entry)}</code>" if entry else f"无法自动判定入口，上传后需在 {len(res.html_paths)} 个 html 中选择"
            return HTMLResponse(
                f'<!DOCTYPE html><html><head><meta charset="utf-8"><link rel="stylesheet" href="{ctx.base_url}/static/viewer.css">'
                '<style>ul{font:13px ui-monospace,Menlo,monospace;columns:2;padding-left:20px}li.entry{font-weight:700;color:#2563eb}.sum{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:14px 18px;margin-bottom:12px}</style></head>'
                f'<body><main><div class="sum"><strong>{html_mod.escape(filename)}</strong> · {size // 1024} KB · 解压 {res.file_count} 个文件（{res.total_bytes // 1024} KB）<br>{verdict}</div><ul>{items}</ul>{more}</main></body></html>'
            )
    except UploadError as e:
        return HTMLResponse(_msg_page(str(e)))
    finally:
        tmp.unlink(missing_ok=True)
        await upload.close()


def _msg_page(msg: str) -> str:
    return f'<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="font:14px sans-serif;padding:30px;color:#991b1b">{html_mod.escape(msg)}</body></html>'


# ---------- 通知测试 ----------

@router.post("/me/notify-test")
async def notify_test(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    ok = await run_in_threadpool(ctx.notifier.send, ctx.user["tg_id"], f"【{ctx.settings.get('site_name') or 'DCPrd'}】测试消息：通知已连通，之后你负责或创建的需求有新版本或留言时会收到提醒。")
    if ok:
        ctx.flash("ok", "测试消息已发送，请查看 Telegram")
    else:
        ctx.flash("err", f"发送失败：请先在 Telegram 里打开 @{ctx.cfg.tg_bot_username} 并点 Start，然后重试")
    return ctx.redirect(str(form.get("back") or "/"))


# ---------- 留言的后台处理 ----------

def comments_for_requirement(ctx: Ctx, req_id: int) -> list:
    return db.all_rows(
        ctx.conn,
        """SELECT c.*, d.name AS doc_name, d.id AS doc_id, u.name AS resolver FROM comments c JOIN documents d ON d.id = c.document_id
           LEFT JOIN users u ON u.id = c.resolved_by
           WHERE d.requirement_id = ? AND c.deleted_at IS NULL ORDER BY c.resolved_at IS NOT NULL, c.id DESC""",
        (req_id,),
    )


def open_comment_counts(ctx: Ctx, req_ids: list[int]) -> dict[int, int]:
    if not req_ids:
        return {}
    marks = ",".join("?" * len(req_ids))
    return {
        r["requirement_id"]: r["n"]
        for r in db.all_rows(
            ctx.conn,
            f"""SELECT d.requirement_id, COUNT(*) AS n FROM comments c JOIN documents d ON d.id = c.document_id
                WHERE d.requirement_id IN ({marks}) AND c.deleted_at IS NULL AND c.resolved_at IS NULL GROUP BY d.requirement_id""",
            req_ids,
        )
    }


@router.post("/comments/{comment_id}/resolve")
async def comment_resolve(comment_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    c = db.one(ctx.conn, "SELECT c.*, d.requirement_id FROM comments c JOIN documents d ON d.id = c.document_id WHERE c.id = ? AND c.deleted_at IS NULL", (comment_id,))
    if not c:
        raise HTTPException(404, "留言不存在")
    if c["resolved_at"]:
        db.update(ctx.conn, "comments", comment_id, {"resolved_at": None, "resolved_by": None})
        ctx.flash("ok", "已重新打开")
    else:
        db.update(ctx.conn, "comments", comment_id, {"resolved_at": db.utcnow(), "resolved_by": ctx.user["id"]})
        ctx.flash("ok", "已标记为已处理")
    return ctx.redirect(str(form.get("back") or f"/req/{c['requirement_id']}"))


@router.post("/comments/{comment_id}/delete")
async def comment_delete(comment_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    c = db.one(ctx.conn, "SELECT c.*, d.requirement_id, r.created_by AS req_creator FROM comments c JOIN documents d ON d.id = c.document_id JOIN requirements r ON r.id = d.requirement_id WHERE c.id = ? AND c.deleted_at IS NULL", (comment_id,))
    if not c:
        raise HTTPException(404, "留言不存在")
    if not (ctx.is_admin or c["req_creator"] == ctx.user["id"]):
        raise HTTPException(403, "只有管理员或需求创建人可以删除留言")
    db.update(ctx.conn, "comments", comment_id, {"deleted_at": db.utcnow(), "deleted_by": ctx.user["id"]})
    db.audit(ctx.conn, ctx.user, "delete_comment", "comment", comment_id, {"author": c["author"], "body": c["body"][:100]})
    ctx.flash("ok", "留言已删除")
    return ctx.redirect(str(form.get("back") or f"/req/{c['requirement_id']}"))
