"""常用查询。"""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from . import db

LATEST_SQL = """
SELECT v.*, u.name AS uploader_name
FROM versions v LEFT JOIN users u ON u.id = v.uploaded_by
WHERE v.document_id = ? AND v.deleted_at IS NULL AND v.entry_path IS NOT NULL
ORDER BY v.number DESC LIMIT 1
"""


def latest_version(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    return db.one(conn, LATEST_SQL, (document_id,))


def version_by_number(conn: sqlite3.Connection, document_id: int, number: int) -> sqlite3.Row | None:
    return db.one(
        conn,
        "SELECT * FROM versions WHERE document_id = ? AND number = ? AND deleted_at IS NULL AND entry_path IS NOT NULL",
        (document_id, number),
    )


def versions_of(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return db.all_rows(
        conn,
        """SELECT v.*, u.name AS uploader_name, s.number AS source_number
           FROM versions v LEFT JOIN users u ON u.id = v.uploaded_by
           LEFT JOIN versions s ON s.id = v.source_version_id
           WHERE v.document_id = ? AND v.deleted_at IS NULL ORDER BY v.number DESC""",
        (document_id,),
    )


def requirement_or_404(conn: sqlite3.Connection, req_id: int) -> sqlite3.Row:
    row = db.one(conn, "SELECT * FROM requirements WHERE id = ? AND deleted_at IS NULL", (req_id,))
    if not row:
        raise HTTPException(404, "需求不存在或已删除")
    return row


def document_or_404(conn: sqlite3.Connection, doc_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    doc = db.one(conn, "SELECT * FROM documents WHERE id = ? AND deleted_at IS NULL", (doc_id,))
    if not doc:
        raise HTTPException(404, "文档不存在或已删除")
    req = db.one(conn, "SELECT * FROM requirements WHERE id = ? AND deleted_at IS NULL", (doc["requirement_id"],))
    if not req:
        raise HTTPException(404, "文档所属需求已删除")
    return doc, req


def version_or_404(conn: sqlite3.Connection, ver_id: int) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    ver = db.one(conn, "SELECT * FROM versions WHERE id = ? AND deleted_at IS NULL", (ver_id,))
    if not ver:
        raise HTTPException(404, "版本不存在或已删除")
    doc, req = document_or_404(conn, ver["document_id"])
    return ver, doc, req


def documents_of(conn: sqlite3.Connection, req_id: int) -> list[dict]:
    docs = db.all_rows(conn, "SELECT * FROM documents WHERE requirement_id = ? AND deleted_at IS NULL ORDER BY position, id", (req_id,))
    return [{"doc": d, "latest": latest_version(conn, d["id"])} for d in docs]


def primary_document(conn: sqlite3.Connection, req_id: int) -> sqlite3.Row | None:
    return db.one(conn, "SELECT * FROM documents WHERE requirement_id = ? AND deleted_at IS NULL ORDER BY position, id LIMIT 1", (req_id,))


def owners_of(conn: sqlite3.Connection, req_id: int) -> list[sqlite3.Row]:
    return db.all_rows(
        conn,
        "SELECT u.* FROM requirement_owners ro JOIN users u ON u.id = ro.user_id WHERE ro.requirement_id = ? ORDER BY u.name",
        (req_id,),
    )


def active_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.all_rows(conn, "SELECT * FROM users WHERE deleted_at IS NULL ORDER BY name")


def user_name(conn: sqlite3.Connection, user_id: int | None) -> str:
    if not user_id:
        return ""
    row = db.one(conn, "SELECT name FROM users WHERE id = ?", (user_id,))
    return row["name"] if row else "（已删除用户）"


def requirement_latest(conn: sqlite3.Connection, req_id: int) -> sqlite3.Row | None:
    """需求下最近一次已发布的版本（用于列表页）。"""
    return db.one(
        conn,
        """SELECT v.*, d.share_code AS doc_code, d.name AS doc_name, u.name AS uploader_name
           FROM versions v JOIN documents d ON d.id = v.document_id LEFT JOIN users u ON u.id = v.uploaded_by
           WHERE d.requirement_id = ? AND d.deleted_at IS NULL AND v.deleted_at IS NULL AND v.entry_path IS NOT NULL
           ORDER BY v.uploaded_at DESC, v.id DESC LIMIT 1""",
        (req_id,),
    )


def touch_requirement(conn: sqlite3.Connection, req_id: int, user_id: int) -> None:
    conn.execute("UPDATE requirements SET updated_at = ?, updated_by = ? WHERE id = ?", (db.utcnow(), user_id, req_id))


TAG_COLORS = ("gray", "red", "orange", "yellow", "green", "teal", "blue", "indigo", "purple", "pink")


def active_tags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.all_rows(conn, "SELECT * FROM tags WHERE deleted_at IS NULL ORDER BY position, name")


def tags_of(conn: sqlite3.Connection, req_id: int) -> list[sqlite3.Row]:
    return db.all_rows(
        conn,
        "SELECT t.* FROM requirement_tags rt JOIN tags t ON t.id = rt.tag_id WHERE rt.requirement_id = ? AND t.deleted_at IS NULL ORDER BY t.position, t.name",
        (req_id,),
    )


def tags_for_requirements(conn: sqlite3.Connection, req_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    if not req_ids:
        return {}
    marks = ",".join("?" * len(req_ids))
    out: dict[int, list] = {rid: [] for rid in req_ids}
    for r in db.all_rows(
        conn,
        f"SELECT rt.requirement_id, t.* FROM requirement_tags rt JOIN tags t ON t.id = rt.tag_id WHERE rt.requirement_id IN ({marks}) AND t.deleted_at IS NULL ORDER BY t.position, t.name",
        req_ids,
    ):
        out[r["requirement_id"]].append(r)
    return out


def set_tags(conn: sqlite3.Connection, req_id: int, tag_ids: list[int]) -> None:
    conn.execute("DELETE FROM requirement_tags WHERE requirement_id = ?", (req_id,))
    for tid in tag_ids:
        if db.one(conn, "SELECT 1 FROM tags WHERE id = ? AND deleted_at IS NULL", (tid,)):
            conn.execute("INSERT OR IGNORE INTO requirement_tags(requirement_id, tag_id) VALUES (?, ?)", (req_id, tid))


def parse_ids(form, key: str) -> list[int]:
    out = []
    for v in form.getlist(key):
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            pass
    return out


# ---------- 项目权限 ----------

def user_levels(conn: sqlite3.Connection, user) -> dict[str, str]:
    """用户在各项目的权限：管理员/超管全部 edit；普通用户按 project_permissions（无记录 = 无权限，不出现在字典里）。"""
    from .web import PROJECTS
    if not user:
        return {}
    if user["role"] in ("admin", "super"):
        return {p: "edit" for p in PROJECTS}
    return {r["project"]: r["level"] for r in db.all_rows(conn, "SELECT project, level FROM project_permissions WHERE user_id = ? AND level IN ('view', 'edit')", (user["id"],)) if r["project"] in PROJECTS}


def set_user_levels(conn: sqlite3.Connection, user_id: int, levels: dict[str, str]) -> None:
    from .web import PROJECTS
    for proj in PROJECTS:
        lv = levels.get(proj, "none")
        if lv in ("view", "edit"):
            conn.execute("INSERT INTO project_permissions(user_id, project, level) VALUES (?, ?, ?) ON CONFLICT(user_id, project) DO UPDATE SET level = excluded.level", (user_id, proj, lv))
        else:
            conn.execute("DELETE FROM project_permissions WHERE user_id = ? AND project = ?", (user_id, proj))


def users_for_project(conn: sqlite3.Connection, project: str) -> list[sqlite3.Row]:
    """可作为该项目需求 / 事项负责人的成员：管理员、超管，以及对该项目至少可见的普通用户。"""
    return db.all_rows(
        conn,
        """SELECT u.* FROM users u WHERE u.deleted_at IS NULL AND (
               u.role IN ('admin', 'super') OR EXISTS (SELECT 1 FROM project_permissions p WHERE p.user_id = u.id AND p.project = ? AND p.level IN ('view', 'edit')))
           ORDER BY u.name""",
        (project,),
    )


def user_projects_map(conn: sqlite3.Connection, restrict_to: list[str] | None = None) -> dict[int, list[str]]:
    """每个成员可见的项目列表（表单里按项目过滤负责人候选用）。restrict_to：只保留这些项目，避免把完整权限矿阵暴露给普通用户。"""
    from .web import PROJECTS
    out: dict[int, list[str]] = {}
    for u in db.all_rows(conn, "SELECT id, role FROM users WHERE deleted_at IS NULL"):
        out[u["id"]] = list(PROJECTS) if u["role"] in ("admin", "super") else []
    for r in db.all_rows(conn, "SELECT user_id, project FROM project_permissions WHERE level IN ('view', 'edit')"):
        if r["user_id"] in out and r["project"] in PROJECTS and r["project"] not in out[r["user_id"]]:
            out[r["user_id"]].append(r["project"])
    if restrict_to is not None:
        keep = set(restrict_to)
        out = {uid: [p for p in ps if p in keep] for uid, ps in out.items()}
    return out
