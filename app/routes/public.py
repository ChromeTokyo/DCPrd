"""公开访问：/s/<code>/... 最新版本；/v/<code>/<n>/... 固定版本；复合需求目录页。"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from .. import db, queries
from ..main import get_ctx
from ..storage import content_file
from ..web import Ctx

router = APIRouter()

SANDBOX_CSP = (
    "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads "
    "allow-popups-to-escape-sandbox allow-top-navigation-by-user-activation"
)
_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)
_EXTRA_TYPES = {
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


def _serve(ctx: Ctx, path: Path, cache: str) -> Response:
    ctype = _content_type(path)
    resp = FileResponse(str(path), media_type=ctype)
    resp.headers["content-type"] = ctype  # 覆盖 Starlette 自动补的 charset
    resp.headers["cache-control"] = cache
    if ctype.startswith("text/html") and ctx.sandbox_enabled:
        resp.headers["content-security-policy"] = SANDBOX_CSP
    return resp


def _doc_by_code(ctx: Ctx, code: str):
    return db.one(
        ctx.conn,
        """SELECT d.* FROM documents d JOIN requirements r ON r.id = d.requirement_id
           WHERE d.share_code = ? AND d.deleted_at IS NULL AND r.deleted_at IS NULL""",
        (code,),
    )


def _req_by_code(ctx: Ctx, code: str):
    return db.one(ctx.conn, "SELECT * FROM requirements WHERE share_code = ? AND deleted_at IS NULL AND kind = 'compound'", (code,))


def _serve_version(ctx: Ctx, doc, ver, rel: str, prefix: str, cache: str) -> Response:
    if rel == "":
        entry = ver["entry_path"]
        if "/" in entry:
            return RedirectResponse(f"{prefix}{entry}", status_code=302)
        rel = entry
    path = content_file(ctx.cfg, doc["id"], ver["number"], rel)
    if path is None:
        # 目录访问：尝试 index.html
        if rel.endswith("/"):
            path = content_file(ctx.cfg, doc["id"], ver["number"], rel + "index.html")
        if path is None:
            raise HTTPException(404, "文件不存在")
    return _serve(ctx, path, cache)


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
