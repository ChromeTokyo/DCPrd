"""上传处理：单 html / zip 解包、入口判定、原文件保留、重新发布。"""
from __future__ import annotations

import html as html_mod
import logging
import json
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import unicodedata
import zipfile
from urllib.parse import unquote
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import Config

log = logging.getLogger("dcpm.storage")

CHUNK = 1024 * 1024
MAX_FILES = 100_000
MAX_CANDIDATES = 500
SKIP_BASENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
HTML_EXTS = (".html", ".htm")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MD_EXTS = (".md", ".markdown")
KIND_LABELS = {"html": "HTML", "zip": "ZIP", "image": "图片", "md": "Markdown"}


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
    if lower.endswith(IMAGE_EXTS):
        return "image"
    if lower.endswith(MD_EXTS):
        return "md"
    raise UploadError("只支持 .html / .htm、.zip、图片（.png .jpg .gif .webp）或 .md 文件")


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

NAME_ENCODINGS = ("utf-8", "gbk", "big5", "cp932", "cp949")
_ENC_ALIASES = {"gb2312": "gbk", "gb18030": "gbk", "cp936": "gbk", "big5hkscs": "big5", "cp950": "big5", "shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "euc_kr": "cp949", "euc-kr": "cp949", "utf_8": "utf-8"}


def _raw_name(info: zipfile.ZipInfo) -> bytes | None:
    """未标记 UTF-8 的条目：还原 zipfile 用 cp437 解码前的原始字节。"""
    if info.flag_bits & 0x800:
        return None
    try:
        return info.filename.encode("cp437")
    except UnicodeEncodeError:
        return None


def _char_score(ch: str) -> float:
    """字符"像不像真实文件名"的分值：以各编码标准的一级/常用字区作为常用度代理。"""
    o = ord(ch)
    if o < 0x80:
        return 0.0
    if 0x3040 <= o <= 0x30FF:  # 平假名 / 片假名
        return 1.0
    if 0xAC00 <= o <= 0xD7A3:  # 韩文音节：KS X 1001 常用 2350 字给高分
        try:
            b = ch.encode("cp949")
            return 1.0 if len(b) == 2 and 0xB0 <= b[0] <= 0xC8 else 0.4
        except UnicodeEncodeError:
            return 0.4
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
        for enc, lo, hi in (("gbk", 0xB0, 0xD7), ("big5", 0xA4, 0xC6), ("cp932", 0x88, 0x98)):
            try:
                b = ch.encode(enc)
            except UnicodeEncodeError:
                continue
            if len(b) == 2 and lo <= b[0] <= hi:  # GB2312 一级 / Big5 常用 / JIS 第一水準
                return 1.0
        return 0.3
    if 0xFF61 <= o <= 0xFF9F:  # 半角片假名：真实文件名极少见，几乎都是 cp932 误解码的产物
        return 0.0
    if 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:  # CJK 标点、全角
        return 0.5
    return -1.0  # 其他符号、私用区、制表符号：多为误解码产物


def _text_score(text: str) -> float:
    return sum(_char_score(c) for c in text)


def detect_name_encoding(infos: list[zipfile.ZipInfo]) -> str | None:
    """整包统一判定未标记条目的文件名编码。返回编码名；无未标记条目返回 None（不需要转换）。

    全部 ASCII 也返回 None。多个编码都能严格解码时用 charset-normalizer 仲裁，仍无法判定按 gbk → big5 → cp932 → cp949。
    """
    raws = [r for r in (_raw_name(i) for i in infos) if r is not None]
    if not raws:
        return None
    joined = b"\n".join(raws)
    if all(b < 0x80 for b in joined):
        return None
    candidates = []
    for enc in NAME_ENCODINGS:
        try:
            joined.decode(enc)
            candidates.append(enc)
        except UnicodeDecodeError:
            continue
    if not candidates:
        return "__unknown__"
    if len(candidates) == 1:
        return candidates[0]
    # 多个编码都能"成功"解码（gbk 常对 Big5/cp932 字节误成功）：按解码结果的常用字得分仲裁
    scored = sorted(((_text_score(joined.decode(e)), -NAME_ENCODINGS.index(e), e) for e in candidates), reverse=True)
    if len(scored) == 1 or scored[0][0] - scored[1][0] > 1.0:
        return scored[0][2]
    # 前两名得分接近：charset-normalizer 仲裁，只在它的判定就是这两者之一时采用
    top2 = [scored[0][2], scored[1][2]]
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(joined).best()
        guess = (best.encoding or "").lower().replace("_", "-") if best else ""
        guess = _ENC_ALIASES.get(guess, guess)
        if guess in top2:
            return guess
    except Exception:  # noqa: BLE001
        pass
    for enc in NAME_ENCODINGS:  # 仍无法判定：按 utf-8 → gbk → big5 → cp932 → cp949 取前两名中的第一个
        if enc in top2:
            return enc
    return scored[0][2]


def _decode_name(info: zipfile.ZipInfo, encoding: str | None) -> tuple[str, bool]:
    """按整包判定的编码还原文件名。返回 (名字, 是否无法识别)。"""
    raw = _raw_name(info)
    if raw is None or encoding is None:
        return info.filename, False
    if encoding == "__unknown__":
        return info.filename, any(b >= 0x80 for b in raw)
    try:
        return raw.decode(encoding), False
    except UnicodeDecodeError:
        return info.filename, True


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
class UploadOutcome:
    version_id: int
    needs_entry: bool
    missing_refs: list[str] = field(default_factory=list)
    undecodable_names: int = 0
    name_encoding: str | None = None

    def notice(self) -> str:
        parts = []
        if self.missing_refs:
            parts.append(f"注意：入口页引用的 {len(self.missing_refs)} 个文件在压缩包里找不到（如 {'、'.join(self.missing_refs[:3])}），这些图片/样式不会显示")
        if self.undecodable_names:
            parts.append(f"压缩包内含 {self.undecodable_names} 个无法识别编码的文件名，建议用 7-Zip 或 macOS 重新打包")
        return "；".join(parts)


@dataclass
class ZipResult:
    file_count: int = 0
    total_bytes: int = 0
    html_paths: list[str] = field(default_factory=list)
    name_encoding: str | None = None
    undecodable_names: int = 0  # 文件名编码无法识别的条目数（落盘为乱码）


def extract_zip(zip_path: Path, content_dir: Path, max_total_bytes: int) -> ZipResult:
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise UploadError("不是有效的 zip 文件") from e
    with zf:
        infos = zf.infolist()
        encoding = detect_name_encoding(infos)
        undecodable = 0
        members: list[tuple[zipfile.ZipInfo, str]] = []
        for info in infos:
            if _is_symlink(info):
                continue
            name, bad = _decode_name(info, encoding)
            rel = _normalize_member(unicodedata.normalize("NFC", name))
            if rel is None:
                continue
            if bad:
                undecodable += 1
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

        res = ZipResult(name_encoding=encoding, undecodable_names=undecodable)
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


# ---------- 本地资源引用 ----------

_SKIP_SCHEMES = ("http:", "https:", "//", "data:", "blob:", "mailto:", "tel:", "javascript:", "about:", "#")
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.I)
_ATTR_RE = re.compile(r"""<(img|source|video|audio|iframe|embed|link|script)\b[^>]*?\s(?:src|href|poster|srcset)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I | re.S)
_STYLE_ATTR_RE = re.compile(r"""\sstyle\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I | re.S)
_STYLE_TAG_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.I | re.S)
MAX_REFS = 500
MAX_MISSING_STORED = 200


def _clean_ref(raw: str) -> str | None:
    s = html_mod.unescape((raw or "").strip())
    if not s:
        return None
    low = s.lower()
    if any(low.startswith(x) for x in _SKIP_SCHEMES):
        return None
    if re.match(r"^[a-z][a-z0-9+.\-]*:", low):  # 其他协议
        return None
    s = s.split("#", 1)[0].split("?", 1)[0]
    s = unquote(s).replace("\\", "/")
    s = unicodedata.normalize("NFC", s)
    return s or None


def _srcset_urls(value: str) -> list[str]:
    return [part.strip().split()[0] for part in value.split(",") if part.strip()]


def _decode_html(html_bytes: bytes) -> str:
    m = re.search(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", html_bytes[:4096], re.I)
    for enc in ([m.group(1).decode("ascii", "ignore")] if m else []) + ["utf-8", "gbk", "big5", "cp932"]:
        try:
            return html_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return html_bytes.decode("utf-8", errors="replace")


def scan_local_refs(html_bytes: bytes) -> list[str]:
    """提取 HTML 中引用的本地相对资源（去重、保序、URL 解码、去 query/hash）。以 / 开头的根路径也算本地引用。"""
    text = _decode_html(html_bytes)
    found: list[str] = []

    def add(raw: str | None):
        r = _clean_ref(raw) if raw else None
        if r and r not in found and len(found) < MAX_REFS:
            found.append(r)

    def scan_css(css: str):
        for m in _CSS_URL_RE.finditer(css or ""):
            add(m.group(2))

    try:
        import html5lib

        doc = html5lib.parse(text, treebuilder="etree", namespaceHTMLElements=False)
        for el in doc.iter():
            tag = el.tag.lower() if isinstance(el.tag, str) else ""
            if tag in ("img", "source"):
                add(el.get("src"))
                for u in _srcset_urls(el.get("srcset") or ""):
                    add(u)
            elif tag in ("video", "audio", "iframe", "embed"):
                add(el.get("src"))
                add(el.get("poster"))
            elif tag == "link":
                rel = (el.get("rel") or "").lower().split()
                if "stylesheet" in rel or "icon" in rel:
                    add(el.get("href"))
            elif tag == "script":
                add(el.get("src"))
            elif tag == "style":
                scan_css(el.text or "")
            if el.get("style"):
                scan_css(el.get("style"))
    except Exception:  # noqa: BLE001  解析失败退回正则
        for m in _ATTR_RE.finditer(text):
            val = m.group(2) or m.group(3) or m.group(4) or ""
            if "," in val and " " in val.strip():
                for u in _srcset_urls(val):
                    add(u)
            else:
                add(val)
        for m in _STYLE_ATTR_RE.finditer(text):
            scan_css(m.group(1) or m.group(2) or "")
        for m in _STYLE_TAG_RE.finditer(text):
            scan_css(m.group(1))
    return found


def describe_refs(refs: list[str], limit: int = 5) -> str:
    shown = "、".join(refs[:limit]) + ("、…" if len(refs) > limit else "")
    roots = [r for r in refs if r.startswith("/")]
    extra = f"（其中 {len(roots)} 个是以 / 开头的根路径，托管后必然 404）" if roots else ""
    return f"{len(refs)} 个本地文件（例如 {shown}）{extra}"


def html_only_error(refs: list[str]) -> str:
    return f"这个 HTML 引用了 {describe_refs(refs)}，单独上传会导致图片/样式无法显示。请把 HTML 和这些文件夹一起压缩成 zip 上传；若确认只传 HTML，请勾选下方确认框。"


def find_missing_refs(content_dir: Path, entry_path: str, refs: list[str]) -> list[str]:
    """检查引用是否存在于 content_dir（NFC 归一化、大小写敏感）。返回缺失清单，大小写不一致时附注实际路径。"""
    root = content_dir.resolve()
    # 精确路径索引：不依赖文件系统是否区分大小写（macOS 默认不区分，Linux 区分）
    exact = {unicodedata.normalize("NFC", p.relative_to(root).as_posix()) for p in root.rglob("*") if p.is_file()}
    lower_index: dict[str, str] | None = None
    missing: list[str] = []
    base = posixpath.dirname(entry_path)
    for ref in refs:
        ref = unicodedata.normalize("NFC", ref)
        if ref.startswith("/"):
            missing.append(f"{ref}（根路径，托管后必然 404）")
            continue
        rel = posixpath.normpath(posixpath.join(base, ref)) if base else posixpath.normpath(ref)
        if rel.startswith(".."):
            missing.append(f"{ref}（超出压缩包范围）")
            continue
        if rel in exact:
            continue
        if lower_index is None:
            lower_index = {x.lower(): x for x in exact}
        hit = lower_index.get(rel.lower())
        missing.append(f"{ref}（注意大小写：实际为 {hit}）" if hit else ref)
        if len(missing) >= MAX_MISSING_STORED:
            break
    return missing


def missing_refs_for_entry(content_dir: Path, entry_path: str) -> list[str]:
    entry = content_dir / entry_path
    if not entry.is_file():
        return []
    try:
        refs = scan_local_refs(entry.read_bytes()[: 8 * 1024 * 1024])
    except Exception:  # noqa: BLE001
        return []
    return find_missing_refs(content_dir, entry_path, refs)


def set_missing_refs(conn: sqlite3.Connection, version_id: int, missing: list[str]) -> None:
    db.update(conn, "versions", version_id, {"missing_refs": json.dumps(missing[:MAX_MISSING_STORED], ensure_ascii=False)})


def ensure_missing_refs(cfg: Config, conn: sqlite3.Connection, versions) -> None:
    """历史版本懒计算：missing_refs 为空且已有入口的版本补扫一次。"""
    for v in versions:
        if v["missing_refs"] is None and v["entry_path"]:
            content_dir = version_dir(cfg, v["document_id"], v["number"]) / "content"
            set_missing_refs(conn, v["id"], missing_refs_for_entry(content_dir, v["entry_path"]))


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
    force: bool = False,
) -> UploadOutcome:
    """把已落盘的临时上传文件处理成一个版本。单 html 引用本地文件时默认拒绝（force=True 放行）。"""
    filename = sanitize_filename(original_filename)
    kind = detect_kind(filename)
    missing: list[str] = []
    undecodable = 0
    name_encoding = None
    if kind == "html":
        refs = scan_local_refs(tmp_path.read_bytes()[: 8 * 1024 * 1024])
        if refs and not force:
            raise UploadError(html_only_error(refs))
        missing = [f"{r}（根路径）" if r.startswith("/") else r for r in refs][:MAX_MISSING_STORED]
    number = next_version_number(conn, document_id)
    vdir = version_dir(cfg, document_id, number)
    if vdir.exists():
        shutil.rmtree(vdir)
    content_dir = vdir / "content"
    original_dir = vdir / "original"
    try:
        content_dir.mkdir(parents=True)
        original_dir.mkdir(parents=True)
        if kind in ("html", "image", "md"):
            shutil.copyfile(tmp_path, content_dir / filename)
            entry, candidates, file_count = filename, None, 1
        else:
            res = extract_zip(tmp_path, content_dir, max_total_bytes=max_upload_bytes * 4)
            if not res.html_paths:
                raise UploadError("压缩包内没有 html 文件")
            entry = decide_entry(res.html_paths)
            candidates = None if entry else json.dumps(res.html_paths[:MAX_CANDIDATES], ensure_ascii=False)
            file_count = res.file_count
            undecodable, name_encoding = res.undecodable_names, res.name_encoding
            if entry:
                missing = find_missing_refs(content_dir, entry, scan_local_refs((content_dir / entry).read_bytes()[: 8 * 1024 * 1024]))
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
            "missing_refs": json.dumps(missing, ensure_ascii=False) if entry else None,
        },
    )
    return UploadOutcome(vid, entry is None, missing, undecodable, name_encoding)


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
            "missing_refs": src["missing_refs"],
            "uploaded_by": user_id,
            "uploaded_at": db.utcnow(),
        },
    )


def content_file(cfg: Config, document_id: int, number: int, rel_path: str) -> Path | None:
    """把公开访问的相对路径解析到 content/ 内的真实文件；越界或不存在返回 None。"""
    content_dir = version_dir(cfg, document_id, number) / "content"
    rel = posixpath.normpath("/" + unicodedata.normalize("NFC", rel_path).replace("\\", "/")).lstrip("/")
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
