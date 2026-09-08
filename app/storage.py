"""上传处理：单 html / zip 解包、入口判定、原文件保留、重新发布。"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import Config

CHUNK = 1024 * 1024
MAX_FILES = 100_000
MAX_CANDIDATES = 500
SKIP_BASENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
HTML_EXTS = (".html", ".htm")


class UploadError(Exception):
    """用户可见的上传错误。"""


class TooLarge(UploadError):
    pass


def sanitize_filename(name: str) -> str:
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    if name in ("", ".", ".."):
        name = "upload"
    return name[:200]


def detect_kind(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(HTML_EXTS):
        return "html"
    if lower.endswith(".zip"):
        return "zip"
    raise UploadError("只支持 .html / .htm 或 .zip 文件")


def copy_stream_limited(src, dest: Path, max_bytes: int) -> int:
    """边读边写并计数，超限即中止并删除临时文件。src 为具有 read() 的对象。"""
    total = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise TooLarge(f"文件超过上传上限（{max_bytes // (1024 * 1024)} MB）")
            out.write(chunk)
    return total


# ---------- zip ----------

def _decode_name(info: zipfile.ZipInfo) -> str:
    """还原 zipfile 用 cp437 解码的文件名，依次尝试 utf-8、gbk。"""
    if info.flag_bits & 0x800:  # 已标记 UTF-8
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def _normalize_member(name: str) -> str | None:
    """返回规范化的相对路径；非法（绝对路径、..）抛错；应跳过的返回 None。"""
    name = name.replace("\\", "/")
    if name.endswith("/"):
        return None  # 目录条目
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise UploadError(f"压缩包含有绝对路径条目：{name}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UploadError(f"压缩包含有非法路径条目：{name}")
    if not parts:
        return None
    if parts[0] == "__MACOSX":
        return None
    if parts[-1] in SKIP_BASENAMES:
        return None
    return "/".join(parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode) if mode else False


@dataclass
class ZipResult:
    file_count: int = 0
    total_bytes: int = 0
    html_paths: list[str] = field(default_factory=list)


def extract_zip(zip_path: Path, content_dir: Path, max_total_bytes: int) -> ZipResult:
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise UploadError("不是有效的 zip 文件") from e
    with zf:
        members: list[tuple[zipfile.ZipInfo, str]] = []
        for info in zf.infolist():
            if _is_symlink(info):
                continue
            rel = _normalize_member(_decode_name(info))
            if rel is None:
                continue
            members.append((info, rel))
        if not members:
            raise UploadError("压缩包内没有可用文件")
        if len(members) > MAX_FILES:
            raise UploadError(f"压缩包文件数超过上限（{MAX_FILES}）")
        declared = sum(i.file_size for i, _ in members)
        if declared > max_total_bytes:
            raise UploadError("解压后总大小超过上限")

        # 所有条目都在同一顶层目录下 → 去掉这一层
        tops = {rel.split("/", 1)[0] for _, rel in members}
        if len(tops) == 1 and all("/" in rel for _, rel in members):
            prefix_len = len(next(iter(tops))) + 1
            members = [(i, rel[prefix_len:]) for i, rel in members]

        # 去重（同名条目后者覆盖）
        seen: dict[str, zipfile.ZipInfo] = {}
        for info, rel in members:
            seen[rel] = info

        res = ZipResult()
        content_dir.mkdir(parents=True, exist_ok=True)
        root = content_dir.resolve()
        for rel, info in seen.items():
            dest = (content_dir / rel)
            if not dest.resolve().is_relative_to(root):
                raise UploadError(f"压缩包含有非法路径条目：{rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    res.total_bytes += len(chunk)
                    if res.total_bytes > max_total_bytes:
                        raise UploadError("解压后总大小超过上限")
                    out.write(chunk)
            res.file_count += 1
            if rel.lower().endswith(HTML_EXTS):
                res.html_paths.append(rel)
        res.html_paths.sort()
        return res


def decide_entry(html_paths: list[str]) -> str | None:
    """入口判定：根 index.html → 根目录唯一 html → 全包唯一 html → None。"""
    root_htmls = [p for p in html_paths if "/" not in p]
    for p in root_htmls:
        if p.lower() in ("index.html", "index.htm"):
            return p
    if len(root_htmls) == 1:
        return root_htmls[0]
    if len(html_paths) == 1:
        return html_paths[0]
    return None


# ---------- 版本落盘 ----------

def version_dir(cfg: Config, document_id: int, number: int) -> Path:
    return cfg.docs_dir / str(document_id) / f"v{number}"


def next_version_number(conn: sqlite3.Connection, document_id: int) -> int:
    row = conn.execute("SELECT COALESCE(MAX(number), 0) AS m FROM versions WHERE document_id = ?", (document_id,)).fetchone()
    return int(row["m"]) + 1


def create_version_from_upload(
    cfg: Config,
    conn: sqlite3.Connection,
    document_id: int,
    uploaded_by: int,
    original_filename: str,
    tmp_path: Path,
    size_bytes: int,
    note: str,
    max_upload_bytes: int,
) -> tuple[int, bool]:
    """把已落盘的临时上传文件处理成一个版本。返回 (version_id, 是否需要选择入口)。"""
    filename = sanitize_filename(original_filename)
    kind = detect_kind(filename)
    number = next_version_number(conn, document_id)
    vdir = version_dir(cfg, document_id, number)
    if vdir.exists():
        shutil.rmtree(vdir)
    content_dir = vdir / "content"
    original_dir = vdir / "original"
    try:
        content_dir.mkdir(parents=True)
        original_dir.mkdir(parents=True)
        if kind == "html":
            shutil.copyfile(tmp_path, content_dir / filename)
            entry, candidates, file_count = filename, None, 1
        else:
            res = extract_zip(tmp_path, content_dir, max_total_bytes=max_upload_bytes * 4)
            if not res.html_paths:
                raise UploadError("压缩包内没有 html 文件")
            entry = decide_entry(res.html_paths)
            candidates = None if entry else json.dumps(res.html_paths[:MAX_CANDIDATES], ensure_ascii=False)
            file_count = res.file_count
        shutil.move(str(tmp_path), original_dir / filename)
    except Exception:
        shutil.rmtree(vdir, ignore_errors=True)
        tmp_path.unlink(missing_ok=True)
        raise
    vid = db.insert(
        conn,
        "versions",
        {
            "document_id": document_id,
            "number": number,
            "kind": kind,
            "original_filename": filename,
            "size_bytes": size_bytes,
            "file_count": file_count,
            "entry_path": entry,
            "html_candidates": candidates,
            "note": note.strip(),
            "source_version_id": None,
            "uploaded_by": uploaded_by,
            "uploaded_at": db.utcnow(),
        },
    )
    return vid, entry is None


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def republish_version(cfg: Config, conn: sqlite3.Connection, src: sqlite3.Row, user_id: int) -> int:
    """以历史版本为来源复制出一个新的最新版本。"""
    number = next_version_number(conn, src["document_id"])
    src_dir = version_dir(cfg, src["document_id"], src["number"])
    dst_dir = version_dir(cfg, src["document_id"], number)
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(src_dir, dst_dir, copy_function=_link_or_copy)
    return db.insert(
        conn,
        "versions",
        {
            "document_id": src["document_id"],
            "number": number,
            "kind": src["kind"],
            "original_filename": src["original_filename"],
            "size_bytes": src["size_bytes"],
            "file_count": src["file_count"],
            "entry_path": src["entry_path"],
            "html_candidates": src["html_candidates"],
            "note": f"重新发布自 v{src['number']}",
            "source_version_id": src["id"],
            "uploaded_by": user_id,
            "uploaded_at": db.utcnow(),
        },
    )


def content_file(cfg: Config, document_id: int, number: int, rel_path: str) -> Path | None:
    """把公开访问的相对路径解析到 content/ 内的真实文件；越界或不存在返回 None。"""
    content_dir = version_dir(cfg, document_id, number) / "content"
    rel = posixpath.normpath("/" + rel_path.replace("\\", "/")).lstrip("/")
    if rel in ("", "."):
        return None
    target = (content_dir / rel)
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_relative_to(content_dir.resolve()):
        return None
    if not resolved.is_file():
        return None
    return resolved
