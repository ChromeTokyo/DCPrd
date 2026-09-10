"""公开访问：/s/<code>/... 最新版本；/v/<code>/<n>/... 固定版本；复合需求目录页。"""
from __future__ import annotations

import html as html_mod
import mimetypes
import re
from urllib.parse import quote

import markdown
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .. import db, queries
from ..public_widget import inject_widget
from ..web import PROJECTS, split_jira_keys
from ..main import get_ctx
from ..storage import IMAGE_EXTS, MD_EXTS, content_file
from ..web import Ctx

router = APIRouter()

SANDBOX_CSP = (
    "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads "
    "allow-popups-to-escape-sandbox allow-top-navigation-by-user-activation"
)
_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)
_EXTRA_TYPES = {
    ".md": "text/markdown", ".markdown": "text/markdown",
    ".js": "application/javascript", ".mjs": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf", ".json": "application/json",
    ".webp": "image/webp", ".ico": "image/x-icon", ".map": "application/json", ".wasm": "application/wasm",
}


def _content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".html", ".htm"):
        charset = "utf-8"
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
            m = _CHARSET_RE.search(head)
            if m:
                charset = m.group(1).decode("ascii").lower()
        except OSError:
            pass
        return f"text/html; charset={charset}"
    if ext in _EXTRA_TYPES:
        return _EXTRA_TYPES[ext]
    guess, _ = mimetypes.guess_type(str(path))
    if guess and guess.startswith("text/"):
        return f"{guess}; charset=utf-8"
    return guess or "application/octet-stream"


def _serve(ctx: Ctx, path: Path, cache: str, widget: dict | None = None) -> Response:
    ctype = _content_type(path)
    if widget is not None and ctype.startswith("text/html"):
        body = inject_widget(path.read_bytes(), widget)
        resp: Response = Response(body, media_type=ctype)
    else:
        resp = FileResponse(str(path), media_type=ctype)
    resp.headers["content-type"] = ctype  # 覆盖 Starlette 自动补的 charset
    resp.headers["cache-control"] = cache
    if ctype.startswith("text/html") and ctx.sandbox_enabled:
        resp.headers["content-security-policy"] = SANDBOX_CSP
    return resp


def _widget_data(ctx: Ctx, doc, ver) -> dict | None:
    """入口页小菜单的数据：需求名、负责人、更新时间、历史版本。"""
    if (ctx.settings.get("public_widget_enabled") or "1") != "1":
        return None
    req = db.one(ctx.conn, "SELECT * FROM requirements WHERE id = ?", (doc["requirement_id"],))
    if not req:
        return None
    owners = [o["name"] for o in queries.owners_of(ctx.conn, req["id"])]
    versions = [
        v for v in db.all_rows(
            ctx.conn,
            """SELECT v.number, v.uploaded_at, v.note, u.name AS uploader FROM versions v LEFT JOIN users u ON u.id = v.uploaded_by
               WHERE v.document_id = ? AND v.deleted_at IS NULL AND v.entry_path IS NOT NULL ORDER BY v.number DESC""",
            (doc["id"],),
        )
    ]
    latest = versions[0]["number"] if versions else ver["number"]
    jira_base = (ctx.settings.get("jira_base_url") or ctx.cfg.jira_base_url).rstrip("/")
    return {
        "req": {
            "name": req["name"], "project": req["project"], "projectLabel": PROJECTS.get(req["project"], req["project"].upper()),
            "owners": owners, "jira": [{"key": k, "url": f"{jira_base}/browse/{k}"} for k in split_jira_keys(req["jira_keys"])],
        },
        "doc": {"name": doc["name"], "compound": req["kind"] == "compound", "dirUrl": f"/s/{req['share_code']}/" if req["kind"] == "compound" else None},
        "current": ver["number"],
        "latest": latest,
        "latestUrl": f"/s/{doc['share_code']}/",
        "commentsUrl": f"/s/{doc['share_code']}/__comments",
        "updated": ctx.fmt_dt(versions[0]["uploaded_at"]) if versions else "",
        "versions": [{"n": v["number"], "time": ctx.fmt_dt(v["uploaded_at"]), "by": v["uploader"] or "", "note": v["note"] or "", "url": f"/v/{doc['share_code']}/{v['number']}/"} for v in versions],
    }


def _doc_by_code(ctx: Ctx, code: str):
    return db.one(
        ctx.conn,
        """SELECT d.* FROM documents d JOIN requirements r ON r.id = d.requirement_id
           WHERE d.share_code = ? AND d.deleted_at IS NULL AND r.deleted_at IS NULL""",
        (code,),
    )


def _req_by_code(ctx: Ctx, code: str):
    return db.one(ctx.conn, "SELECT * FROM requirements WHERE share_code = ? AND deleted_at IS NULL AND kind = 'compound'", (code,))


def _render_viewer(ctx: Ctx, doc, ver, path: Path, cache: str, widget: dict | None) -> Response:
    """图片 / Markdown 入口：包一层干净的查看页。"""
    name = html_mod.escape(doc["name"])
    file_url = quote(ver["entry_path"], safe="/")
    if ver["kind"] == "md":
        text = path.read_text("utf-8", errors="replace")
        body = markdown.markdown(text, extensions=["extra", "tables", "fenced_code", "sane_lists", "toc", "nl2br"], output_format="html5")
        inner = f'<article class="md">{body}</article>'
        cls = "view-md"
    else:
        inner = f'<figure class="pic"><img src="{file_url}" alt="{name}"></figure>'
        cls = "view-image"
    page = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{name}</title>" '<link rel="stylesheet" href="/static/viewer.css"></head>'
        f'<body class="{cls}"><main>{inner}</main></body></html>'
    ).encode("utf-8")
    if widget is not None:
        page = inject_widget(page, widget)
    resp = Response(page, media_type="text/html")
    resp.headers["content-type"] = "text/html; charset=utf-8"
    resp.headers["cache-control"] = cache
    if ctx.sandbox_enabled:
        resp.headers["content-security-policy"] = SANDBOX_CSP
    return resp


def _serve_version(ctx: Ctx, doc, ver, rel: str, prefix: str, cache: str) -> Response:
    is_root = rel == ""
    if is_root:
        entry = ver["entry_path"]
        if "/" in entry:
            return RedirectResponse(f"{prefix}{entry}", status_code=302)
        rel = entry
    is_entry = rel == ver["entry_path"]
    path = content_file(ctx.cfg, doc["id"], ver["number"], rel)
    if ver["kind"] in ("image", "md"):
        # 根地址给查看页（带小菜单）；直接访问文件名则返回原始文件
        if is_root and path is not None:
            return _render_viewer(ctx, doc, ver, path, cache, _widget_data(ctx, doc, ver))
        widget = None
    else:
        widget = _widget_data(ctx, doc, ver) if is_entry else None
    if path is None:
        # 目录访问：尝试 index.html
        if rel.endswith("/"):
            path = content_file(ctx.cfg, doc["id"], ver["number"], rel + "index.html")
        if path is None:
            raise HTTPException(404, "文件不存在")
    return _serve(ctx, path, cache, widget)


# ---------- 公开留言（免登录；页面在沙箱内为 opaque origin，需允许跨域） ----------
_CORS = {"access-control-allow-origin": "*", "cache-control": "no-store"}
_rate: dict[str, list[float]] = {}
COMMENT_LIMIT_PER_10MIN = 10


def _rate_ok(ip: str) -> bool:
    import time as _t
    now = _t.time()
    hits = [t for t in _rate.get(ip, []) if now - t < 600]
    if len(hits) >= COMMENT_LIMIT_PER_10MIN:
        _rate[ip] = hits
        return False
    hits.append(now)
    _rate[ip] = hits
    return True


def _comment_json(ctx: Ctx, c) -> dict:
    return {"id": c["id"], "author": c["author"], "body": c["body"], "version": c["version_number"], "time": ctx.fmt_dt(c["created_at"]), "resolved": bool(c["resolved_at"])}


@router.get("/s/{code}/__comments")
async def public_comments_list(code: str, ctx: Ctx = Depends(get_ctx)):
    doc = _doc_by_code(ctx, code)
    if not doc:
        raise HTTPException(404, "链接无效")
    rows = db.all_rows(ctx.conn, "SELECT * FROM comments WHERE document_id = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 200", (doc["id"],))
    return JSONResponse([_comment_json(ctx, c) for c in rows], headers=_CORS)


@router.post("/s/{code}/__comments")
async def public_comments_create(code: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    doc = _doc_by_code(ctx, code)
    if not doc:
        raise HTTPException(404, "链接无效")
    form = await request.form()
    author = str(form.get("author") or "").strip()[:40]
    body = str(form.get("body") or "").strip()[:2000]
    version = str(form.get("version") or "")
    if not author or not body:
        return JSONResponse({"error": "请填写名字和留言内容"}, status_code=400, headers=_CORS)
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "") or "").split(",")[0].strip()
    if not _rate_ok(ip or "?"):
        return JSONResponse({"error": "留言太频繁，请稍后再试"}, status_code=429, headers=_CORS)
    cid = db.insert(ctx.conn, "comments", {"document_id": doc["id"], "version_number": int(version) if version.isdigit() else None, "author": author, "body": body, "ip": ip, "created_at": db.utcnow()})
    req = db.one(ctx.conn, "SELECT * FROM requirements WHERE id = ?", (doc["requirement_id"],))
    if req:
        from ..notify import notify_requirement
        title = req["name"] if req["kind"] == "single" else f"{req['name']} · {doc['name']}"
        notify_requirement(ctx.conn, ctx.notifier, req["id"], f"【{ctx.settings.get('site_name') or 'DCPrd'}】{author} 在「{title}」留言：\n{body[:300]}\n{ctx.base_url}/req/{req['id']}#comments")
    c = db.one(ctx.conn, "SELECT * FROM comments WHERE id = ?", (cid,))
    return JSONResponse(_comment_json(ctx, c), headers=_CORS)


@router.get("/s/{code}")
async def share_noslash(code: str):
    return RedirectResponse(f"/s/{code}/", status_code=301)


@router.get("/s/{code}/{rel:path}")
async def share_latest(code: str, rel: str = "", ctx: Ctx = Depends(get_ctx)):
    doc = _doc_by_code(ctx, code)
    if doc:
        ver = queries.latest_version(ctx.conn, doc["id"])
        if not ver:
            raise HTTPException(404, "该文档尚无已发布版本")
        return _serve_version(ctx, doc, ver, rel, f"/s/{code}/", "no-cache")
    req = _req_by_code(ctx, code)
    if req and rel == "":
        docs = [d for d in queries.documents_of(ctx.conn, req["id"])]
        resp = ctx.render("public_dir.html", req=req, docs=docs)
        resp.headers["cache-control"] = "no-cache"
        return resp
    raise HTTPException(404, "链接无效或已失效")


@router.get("/v/{code}/{number}")
async def version_noslash(code: str, number: int):
    return RedirectResponse(f"/v/{code}/{number}/", status_code=301)


@router.get("/v/{code}/{number}/{rel:path}")
async def share_version(code: str, number: int, rel: str = "", ctx: Ctx = Depends(get_ctx)):
    doc = _doc_by_code(ctx, code)
    if not doc:
        raise HTTPException(404, "链接无效或已失效")
    ver = queries.version_by_number(ctx.conn, doc["id"], number)
    if not ver:
        raise HTTPException(404, "版本不存在")
    return _serve_version(ctx, doc, ver, rel, f"/v/{code}/{number}/", "public, max-age=31536000, immutable")
