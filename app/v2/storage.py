"""2.0 版本内容落盘与 diff 计算。"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from .. import db
from ..config import Config
from .diff_html import diff_html
from .diff_image import diff_images

HTML_EXTS = (".html", ".htm")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


class V2Error(Exception):
    pass


def media_for(filename: str, expected: str | None = None) -> str:
    low = filename.lower()
    if low.endswith(HTML_EXTS):
        media = "html"
    elif low.endswith(IMAGE_EXTS):
        media = "image"
    else:
        raise V2Error("只支持 .html/.htm 或 .png/.jpg/.webp 文件")
    if expected and media != expected:
        raise V2Error(f"该页面为 {'截图' if expected == 'image' else 'HTML'} 类型，请上传对应格式的文件")
    return media


def version_dir(cfg: Config, page_id: int, version_id: int) -> Path:
    return cfg.data_dir / "v2" / "pages" / str(page_id) / str(version_id)


def content_path(cfg: Config, ver: sqlite3.Row) -> Path:
    d = version_dir(cfg, ver["page_id"], ver["id"])
    ext = ".html" if ver["media"] == "html" else Path(ver["original_filename"] or "x.png").suffix.lower() or ".png"
    return d / f"page{ext}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def latest_baseline(conn: sqlite3.Connection, page_id: int) -> sqlite3.Row | None:
    return db.one(conn, "SELECT * FROM page_versions WHERE page_id = ? AND kind = 'baseline' AND deleted_at IS NULL ORDER BY id DESC LIMIT 1", (page_id,))


def create_version(
    cfg: Config,
    conn: sqlite3.Connection,
    page_id: int,
    kind: str,
    media: str,
    src: Path,
    original_filename: str,
    uploaded_by: int | None,
    release_id: int | None = None,
    requirement_id: int | None = None,
    base_version_id: int | None = None,
    note: str = "",
    content_hash: str | None = None,
) -> int:
    content_hash = content_hash or sha256_file(src)
    vid = db.insert(
        conn,
        "page_versions",
        {
            "page_id": page_id, "kind": kind, "media": media, "release_id": release_id, "requirement_id": requirement_id,
            "base_version_id": base_version_id, "content_hash": content_hash, "original_filename": original_filename,
            "size_bytes": src.stat().st_size, "note": note, "uploaded_by": uploaded_by, "uploaded_at": db.utcnow(),
        },
    )
    ver = db.one(conn, "SELECT * FROM page_versions WHERE id = ?", (vid,))
    dest = content_path(cfg, ver)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return vid


def compute_diff(cfg: Config, conn: sqlite3.Connection, version_id: int) -> dict | None:
    """对 proposal 版本计算与 base_version 的差异并写入 diff_json。"""
    ver = db.one(conn, "SELECT * FROM page_versions WHERE id = ?", (version_id,))
    if not ver or not ver["base_version_id"]:
        return None
    base = db.one(conn, "SELECT * FROM page_versions WHERE id = ?", (ver["base_version_id"],))
    if not base or base["media"] != ver["media"]:
        result = {"changes": [], "error": "基线版本缺失或类型不同，无法自动对比", "counts": {}, "ratio": 0}
    else:
        try:
            if ver["media"] == "html":
                result = diff_html(content_path(cfg, base).read_text("utf-8", errors="replace"), content_path(cfg, ver).read_text("utf-8", errors="replace"))
            else:
                result = diff_images(str(content_path(cfg, base)), str(content_path(cfg, ver)))
        except Exception as e:  # noqa: BLE001
            result = {"changes": [], "error": f"自动对比失败：{e}", "counts": {}, "ratio": 0}
    db.update(conn, "page_versions", version_id, {"diff_json": json.dumps(result, ensure_ascii=False)})
    return result
