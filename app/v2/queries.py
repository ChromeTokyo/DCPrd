"""2.0 查询辅助。"""
from __future__ import annotations

import json
import sqlite3

from fastapi import HTTPException

from .. import db


def systems_with_apps(conn: sqlite3.Connection, project: str | None = None) -> list[dict]:
    where = "s.deleted_at IS NULL" + (" AND s.project = ?" if project else "")
    systems = db.all_rows(conn, f"SELECT s.* FROM systems s WHERE {where} ORDER BY s.project, s.position, s.id", (project,) if project else ())
    out = []
    for s in systems:
        apps = db.all_rows(
            conn,
            """SELECT a.*, (SELECT COUNT(*) FROM pages p WHERE p.app_id = a.id AND p.deleted_at IS NULL) AS page_count,
                      (SELECT version FROM releases r WHERE r.app_id = a.id ORDER BY r.id DESC LIMIT 1) AS latest_release
               FROM apps a WHERE a.system_id = ? AND a.deleted_at IS NULL ORDER BY a.position, a.id""",
            (s["id"],),
        )
        out.append({"system": s, "apps": apps})
    return out


def app_or_404(conn: sqlite3.Connection, app_id: int) -> sqlite3.Row:
    row = db.one(conn, "SELECT a.*, s.name AS system_name, s.project AS project FROM apps a JOIN systems s ON s.id = a.system_id WHERE a.id = ? AND a.deleted_at IS NULL", (app_id,))
    if not row:
        raise HTTPException(404, "端不存在")
    return row


def page_or_404(conn: sqlite3.Connection, page_id: int) -> sqlite3.Row:
    row = db.one(
        conn,
        """SELECT p.*, a.name AS app_name, a.key AS app_key, a.renderer AS renderer, a.system_id, s.name AS system_name, s.project AS project
           FROM pages p JOIN apps a ON a.id = p.app_id JOIN systems s ON s.id = a.system_id
           WHERE p.id = ? AND p.deleted_at IS NULL AND a.deleted_at IS NULL""",
        (page_id,),
    )
    if not row:
        raise HTTPException(404, "页面不存在")
    return row


def version_or_404(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
    row = db.one(
        conn,
        """SELECT v.*, u.name AS uploader_name, r.version AS release_version, q.name AS req_name, q.jira_keys AS req_jira, q.share_code AS req_share
           FROM page_versions v LEFT JOIN users u ON u.id = v.uploaded_by LEFT JOIN releases r ON r.id = v.release_id
           LEFT JOIN requirements q ON q.id = v.requirement_id
           WHERE v.id = ? AND v.deleted_at IS NULL""",
        (version_id,),
    )
    if not row:
        raise HTTPException(404, "版本不存在")
    return row


def versions_of_page(conn: sqlite3.Connection, page_id: int) -> list[sqlite3.Row]:
    return db.all_rows(
        conn,
        """SELECT v.*, u.name AS uploader_name, r.version AS release_version, q.name AS req_name, q.jira_keys AS req_jira, q.v2_status AS req_status
           FROM page_versions v LEFT JOIN users u ON u.id = v.uploaded_by LEFT JOIN releases r ON r.id = v.release_id
           LEFT JOIN requirements q ON q.id = v.requirement_id
           WHERE v.page_id = ? AND v.deleted_at IS NULL AND (v.requirement_id IS NULL OR q.deleted_at IS NULL)
           ORDER BY v.id""",
        (page_id,),
    )


def latest_version(conn: sqlite3.Connection, page_id: int, kind: str | None = None) -> sqlite3.Row | None:
    sql = "SELECT * FROM page_versions WHERE page_id = ? AND deleted_at IS NULL"
    params: list = [page_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return db.one(conn, sql + " ORDER BY id DESC LIMIT 1", params)


def latest_proposal(conn: sqlite3.Connection, requirement_id: int, page_id: int) -> sqlite3.Row | None:
    return db.one(
        conn,
        "SELECT v.*, u.name AS uploader_name FROM page_versions v LEFT JOIN users u ON u.id = v.uploaded_by WHERE v.requirement_id = ? AND v.page_id = ? AND v.deleted_at IS NULL ORDER BY v.id DESC LIMIT 1",
        (requirement_id, page_id),
    )


def menu_tree(conn: sqlite3.Connection, app_id: int) -> list[dict]:
    rows = db.all_rows(conn, "SELECT * FROM menu_nodes WHERE app_id = ? AND deleted_at IS NULL ORDER BY position, id", (app_id,))
    by_parent: dict = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)
    page_ids = [r["page_id"] for r in rows if r["page_id"]]
    counts = {}
    if page_ids:
        marks = ",".join("?" * len(page_ids))
        for c in db.all_rows(
            conn,
            f"""SELECT v.page_id, COUNT(DISTINCT v.requirement_id) AS n FROM page_versions v JOIN requirements q ON q.id = v.requirement_id
                WHERE v.page_id IN ({marks}) AND v.kind = 'proposal' AND v.deleted_at IS NULL AND q.deleted_at IS NULL GROUP BY v.page_id""",
            page_ids,
        ):
            counts[c["page_id"]] = c["n"]

    def build(parent_id):
        return [{"node": r, "req_count": counts.get(r["page_id"], 0), "children": build(r["id"])} for r in by_parent.get(parent_id, [])]

    return build(None)


def menu_path(conn: sqlite3.Connection, page_id: int) -> list[str]:
    node = db.one(conn, "SELECT * FROM menu_nodes WHERE page_id = ? AND deleted_at IS NULL ORDER BY id LIMIT 1", (page_id,))
    parts: list[str] = []
    depth = 0
    while node and depth < 20:
        parts.append(node["title"])
        node = db.one(conn, "SELECT * FROM menu_nodes WHERE id = ?", (node["parent_id"],)) if node["parent_id"] else None
        depth += 1
    return list(reversed(parts))


def unlisted_pages(conn: sqlite3.Connection, app_id: int) -> list[sqlite3.Row]:
    return db.all_rows(
        conn,
        """SELECT p.* FROM pages p WHERE p.app_id = ? AND p.deleted_at IS NULL
           AND NOT EXISTS (SELECT 1 FROM menu_nodes m WHERE m.page_id = p.id AND m.deleted_at IS NULL) ORDER BY p.title""",
        (app_id,),
    )


def requirement_v2_or_404(conn: sqlite3.Connection, req_id: int) -> sqlite3.Row:
    row = db.one(conn, "SELECT * FROM requirements WHERE id = ? AND kind = 'v2' AND deleted_at IS NULL", (req_id,))
    if not row:
        raise HTTPException(404, "需求不存在或已删除")
    return row


def requirement_pages(conn: sqlite3.Connection, req_id: int) -> list[dict]:
    rows = db.all_rows(
        conn,
        """SELECT rp.*, p.title, p.route_key, p.is_new, p.app_id, a.name AS app_name, a.renderer
           FROM requirement_pages rp JOIN pages p ON p.id = rp.page_id JOIN apps a ON a.id = p.app_id
           WHERE rp.requirement_id = ? AND rp.deleted_at IS NULL AND p.deleted_at IS NULL ORDER BY a.position, rp.added_at, p.id""",
        (req_id,),
    )
    out = []
    for r in rows:
        prop = latest_proposal(conn, req_id, r["page_id"])
        base = db.one(conn, "SELECT v.*, r.version AS release_version FROM page_versions v LEFT JOIN releases r ON r.id = v.release_id WHERE v.id = ?", (r["base_version_id"],)) if r["base_version_id"] else None
        diff = json.loads(prop["diff_json"]) if prop and prop["diff_json"] else None
        out.append({"rp": r, "proposal": prop, "base": base, "diff": diff, "menu_path": menu_path(conn, r["page_id"])})
    return out


def requirement_apps(conn: sqlite3.Connection, req_id: int) -> list[sqlite3.Row]:
    return db.all_rows(
        conn,
        """SELECT ra.*, a.name AS app_name, a.key AS app_key FROM requirement_apps ra JOIN apps a ON a.id = ra.app_id
           WHERE ra.requirement_id = ? ORDER BY a.position, a.id""",
        (req_id,),
    )


def sync_requirement_apps(conn: sqlite3.Connection, req_id: int) -> None:
    """按涉及页面的端补齐 requirement_apps 行（不删除已有）。"""
    for r in db.all_rows(conn, "SELECT DISTINCT p.app_id FROM requirement_pages rp JOIN pages p ON p.id = rp.page_id WHERE rp.requirement_id = ? AND rp.deleted_at IS NULL", (req_id,)):
        conn.execute("INSERT OR IGNORE INTO requirement_apps(requirement_id, app_id, release_version) VALUES (?, ?, '')", (req_id, r["app_id"]))


def annotations_of(conn: sqlite3.Connection, version_id: int) -> list[sqlite3.Row]:
    return db.all_rows(
        conn,
        "SELECT an.*, u.name AS author FROM annotations an LEFT JOIN users u ON u.id = an.created_by WHERE an.version_id = ? AND an.deleted_at IS NULL ORDER BY an.id",
        (version_id,),
    )


def other_pages_in_requirement(conn: sqlite3.Connection, req_id: int, page_id: int) -> list[sqlite3.Row]:
    return db.all_rows(
        conn,
        """SELECT p.id, p.title, p.route_key, a.name AS app_name FROM requirement_pages rp JOIN pages p ON p.id = rp.page_id JOIN apps a ON a.id = p.app_id
           WHERE rp.requirement_id = ? AND rp.page_id != ? AND rp.deleted_at IS NULL AND p.deleted_at IS NULL ORDER BY a.position, p.title""",
        (req_id, page_id),
    )


def search_pages(conn: sqlite3.Connection, q: str, project: str | None = None, limit: int = 30) -> list[sqlite3.Row]:
    like = f"%{q}%"
    sql = """SELECT p.*, a.name AS app_name, s.name AS system_name, s.project FROM pages p JOIN apps a ON a.id = p.app_id JOIN systems s ON s.id = a.system_id
             WHERE p.deleted_at IS NULL AND a.deleted_at IS NULL AND (p.title LIKE ? OR p.route_key LIKE ?)"""
    params: list = [like, like]
    if project:
        sql += " AND s.project = ?"
        params.append(project)
    return db.all_rows(conn, sql + " ORDER BY p.title LIMIT ?", [*params, limit])
