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
