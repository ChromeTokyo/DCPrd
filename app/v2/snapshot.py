"""快照包导入与菜单（路由 JSON）导入。

快照包：zip，根目录 manifest.json：
{
  "app": "eb-ops-admin", "release": "3.12.0", "renderer": "html" | "image",
  "menu": [{"title": "商户管理", "route": "/merchant", "children": [{"title": "商户列表", "route": "/merchant/list"}]}],
  "pages": [{"route": "/merchant/list", "title": "商户列表", "file": "pages/0001.html", "hash": "sha256..."}]
}
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .. import db
from ..config import Config
from .storage import V2Error, create_version, latest_baseline, sha256_file

TITLE_KEYS = ("title", "name", "label", "text")
PATH_KEYS = ("route", "path", "key", "url", "fullPath")
CHILD_KEYS = ("children", "routes", "items", "subMenu")


def _pick(d: dict, keys, nested_meta: bool = True) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    if nested_meta and isinstance(d.get("meta"), dict):
        return _pick(d["meta"], keys, nested_meta=False)
    return ""


def normalize_menu(raw: Any, parent_path: str = "") -> list[dict]:
    """把前端路由/菜单配置（常见形状）规整为 [{title, route, children}]。"""
    if isinstance(raw, dict):
        for k in CHILD_KEYS + ("menu", "data", "routes"):
            if isinstance(raw.get(k), list):
                return normalize_menu(raw[k], parent_path)
        raw = [raw]
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        title = _pick(item, TITLE_KEYS)
        route = _pick(item, PATH_KEYS, nested_meta=False)
        if route and not route.startswith(("/", "#", "http")) and parent_path:
            route = parent_path.rstrip("/") + "/" + route
        children = []
        for k in CHILD_KEYS:
            if isinstance(item.get(k), list):
                children = normalize_menu(item[k], route or parent_path)
                break
        hidden = item.get("hidden") is True or (isinstance(item.get("meta"), dict) and item["meta"].get("hidden") is True)
        if hidden or (not title and not children):
            continue
        out.append({"title": title or route or "(未命名)", "route": route, "children": children})
    return out


def _get_or_create_page(conn: sqlite3.Connection, app_id: int, route: str, title: str, user_id: int | None) -> int:
    row = db.one(conn, "SELECT * FROM pages WHERE app_id = ? AND route_key = ?", (app_id, route))
    if row:
        if row["deleted_at"]:
            db.update(conn, "pages", row["id"], {"deleted_at": None})
        if title and row["title"] != title and not row["is_new"]:
            db.update(conn, "pages", row["id"], {"title": title})
        return row["id"]
    return db.insert(conn, "pages", {"app_id": app_id, "route_key": route, "title": title or route, "created_by": user_id, "created_at": db.utcnow()})


def replace_menu(conn: sqlite3.Connection, app_id: int, menu: list[dict], user_id: int | None) -> int:
    """整棵替换某端的菜单树；叶子节点按 route 绑定/创建页面。返回节点数。"""
    conn.execute("UPDATE menu_nodes SET deleted_at = ? WHERE app_id = ? AND deleted_at IS NULL", (db.utcnow(), app_id))
    count = 0

    def walk(items: list[dict], parent_id: int | None):
        nonlocal count
        for pos, it in enumerate(items):
            page_id = None
            if it.get("route") and not it.get("children"):
                page_id = _get_or_create_page(conn, app_id, it["route"], it["title"], user_id)
            nid = db.insert(conn, "menu_nodes", {"app_id": app_id, "parent_id": parent_id, "title": it["title"], "route_key": it.get("route") or None, "page_id": page_id, "position": pos, "created_at": db.utcnow()})
            count += 1
            if it.get("children"):
                walk(it["children"], nid)

    walk(menu, None)
    return count


def align_requirements(conn: sqlite3.Connection, app_id: int, version: str) -> int:
    """release 导入后：该端计划上线版本等于 version 的需求标记已上线；所有端都上线的需求状态置 live。"""
    now = db.utcnow()
    cur = conn.execute("UPDATE requirement_apps SET live_at = ? WHERE app_id = ? AND release_version = ? AND live_at IS NULL", (now, app_id, version))
    n = cur.rowcount
    conn.execute(
        """UPDATE requirements SET v2_status = 'live' WHERE kind = 'v2' AND deleted_at IS NULL AND v2_status != 'live'
           AND EXISTS (SELECT 1 FROM requirement_apps ra WHERE ra.requirement_id = requirements.id)
           AND NOT EXISTS (SELECT 1 FROM requirement_apps ra WHERE ra.requirement_id = requirements.id AND ra.live_at IS NULL)"""
    )
    return n


def import_snapshot(cfg: Config, conn: sqlite3.Connection, app: sqlite3.Row, zip_path: Path, filename: str, user_id: int | None, release_override: str = "") -> dict:
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise V2Error("不是有效的 zip 文件") from e
    with zf, tempfile.TemporaryDirectory(dir=cfg.tmp_dir) as tmpd:
        names = zf.namelist()
        mf_name = next((n for n in names if n.endswith("manifest.json") and n.count("/") <= 1), None)
        if not mf_name:
            raise V2Error("快照包缺少 manifest.json")
        prefix = mf_name[: -len("manifest.json")]
        try:
            mf = json.loads(zf.read(mf_name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise V2Error(f"manifest.json 解析失败：{e}") from e
        version = (release_override or str(mf.get("release") or "")).strip()
        if not version:
            raise V2Error("缺少 release 版本号")
        renderer = mf.get("renderer") or app["renderer"]
        media = "image" if renderer in ("image", "screenshot") else "html"
        pages = mf.get("pages") or []
        if not isinstance(pages, list) or not pages:
            raise V2Error("manifest.pages 为空")

        rel = db.one(conn, "SELECT * FROM releases WHERE app_id = ? AND version = ?", (app["id"], version))
        if rel:
            release_id = rel["id"]
        else:
            release_id = db.insert(conn, "releases", {"app_id": app["id"], "version": version, "note": str(mf.get("note") or ""), "imported_by": user_id, "imported_at": db.utcnow()})

        if mf.get("menu"):
            replace_menu(conn, app["id"], normalize_menu(mf["menu"]), user_id)

        new_versions = 0
        unchanged = 0
        errors: list[str] = []
        for p in pages:
            route = str(p.get("route") or p.get("path") or "").strip()
            fname = p.get("file")
            if not route or not fname:
                errors.append(f"条目缺少 route/file：{p}")
                continue
            member = prefix + fname if (prefix + fname) in names else (fname if fname in names else None)
            if not member:
                errors.append(f"{route}: 文件不存在 {fname}")
                continue
            page_id = _get_or_create_page(conn, app["id"], route, str(p.get("title") or ""), user_id)
            db.update(conn, "pages", page_id, {"is_new": 0})
            # 保证叶子菜单绑定到页面
            conn.execute("UPDATE menu_nodes SET page_id = ? WHERE app_id = ? AND route_key = ? AND deleted_at IS NULL AND page_id IS NULL", (page_id, app["id"], route))
            ext = Path(fname).suffix.lower() or (".html" if media == "html" else ".png")
            tmp_file = Path(tmpd) / f"{page_id}{ext}"
            with zf.open(member) as src, open(tmp_file, "wb") as out:
                out.write(src.read())
            h = sha256_file(tmp_file)
            last = latest_baseline(conn, page_id)
            if last and last["content_hash"] == h:
                unchanged += 1
                continue
            create_version(cfg, conn, page_id, "baseline", media, tmp_file, Path(fname).name, user_id, release_id=release_id, content_hash=h, note=f"快照 {version}")
            new_versions += 1
        db.insert(conn, "snapshot_imports", {"app_id": app["id"], "release_id": release_id, "filename": filename, "page_count": len(pages), "new_versions": new_versions, "imported_by": user_id, "imported_at": db.utcnow()})
        aligned = align_requirements(conn, app["id"], version)
        return {"release": version, "pages": len(pages), "new_versions": new_versions, "unchanged": unchanged, "errors": errors, "aligned": aligned}
