"""重点事项：新增 / 编辑 / 进展 / 完成 / 附件 / 定时提醒。"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .. import db, queries
from ..config import Config
from ..main import get_ctx
from ..notify import Notifier, notify_users, read_status
from ..storage import copy_stream_limited, sanitize_filename
from ..web import PROJECTS, Ctx

router = APIRouter(prefix="/items")

# 预设间隔（小时）；custom 由用户输入
FREQUENCIES = {
    "daily": ("每日", 24), "every2days": ("每 2 天", 48), "twice_weekly": ("每周 2 次", 84), "weekly": ("每周", 168),
    "biweekly": ("双周", 336), "monthly": ("每月", 720), "custom": ("自定义", None),
}
MIN_HOURS, MAX_HOURS = 1, 24 * 365


def freq_label(frequency: str, hours: int) -> str:
    if frequency in FREQUENCIES and FREQUENCIES[frequency][1] == hours:
        return FREQUENCIES[frequency][0]
    if hours % 24 == 0:
        d = hours // 24
        if d == 7:
            return "每周"
        if 7 % d == 0 and d < 7:
            return f"每 {d} 天（每周 {7 // d} 次）"
        return f"每 {d} 天"
    if 168 % hours == 0:
        return f"每周 {168 // hours} 次"
    return f"每 {hours} 小时"


def parse_interval(form) -> tuple[str, int]:
    """返回 (frequency, interval_hours)。custom 支持：N 天 / N 小时 / 每周 N 次。"""
    frequency = str(form.get("frequency") or "weekly")
    if frequency in FREQUENCIES and frequency != "custom":
        return frequency, FREQUENCIES[frequency][1]
    try:
        value = float(str(form.get("custom_value") or "0"))
    except ValueError:
        value = 0
    unit = str(form.get("custom_unit") or "days")
    if value <= 0:
        raise HTTPException(400, "自定义间隔需要填写大于 0 的数字")
    if unit == "hours":
        hours = value
    elif unit == "per_week":
        hours = 168 / value
    else:
        hours = value * 24
    hours = int(round(hours))
    if not MIN_HOURS <= hours <= MAX_HOURS:
        raise HTTPException(400, "自定义间隔需在 1 小时到 1 年之间")
    return "custom", hours
STATUS = {"open": "进行中", "done": "已完成"}
UPDATE_KINDS = {"progress": "进展", "complete": "标记完成", "reopen": "重新打开", "edit": "修改信息", "remind": "系统提醒", "create": "创建"}


def _iso(d: dt.datetime) -> str:
    return d.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


def next_remind(last_progress_iso: str, hours: int) -> str:
    base = db.parse_utc(last_progress_iso) or dt.datetime.now(dt.timezone.utc)
    return _iso(base + dt.timedelta(hours=int(hours or 168)))


def files_dir(cfg: Config, item_id: int) -> Path:
    return cfg.data_dir / "items" / str(item_id)


def item_or_404(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    row = db.one(conn, "SELECT * FROM key_items WHERE id = ? AND deleted_at IS NULL", (item_id,))
    if not row:
        raise HTTPException(404, "重点事项不存在或已删除")
    return row


def people(conn: sqlite3.Connection, item_id: int, role: str) -> list[sqlite3.Row]:
    return db.all_rows(conn, "SELECT u.* FROM key_item_people p JOIN users u ON u.id = p.user_id WHERE p.item_id = ? AND p.role = ? ORDER BY u.name", (item_id, role))


def people_ids(conn: sqlite3.Connection, item_id: int) -> list[int]:
    return [r["user_id"] for r in db.all_rows(conn, "SELECT DISTINCT user_id FROM key_item_people WHERE item_id = ?", (item_id,))]


def _set_people(conn, item_id: int, role: str, ids: list[int]) -> None:
    conn.execute("DELETE FROM key_item_people WHERE item_id = ? AND role = ?", (item_id, role))
    for uid in dict.fromkeys(ids):
        if db.one(conn, "SELECT 1 FROM users WHERE id = ? AND deleted_at IS NULL", (uid,)):
            conn.execute("INSERT OR IGNORE INTO key_item_people(item_id, user_id, role) VALUES (?, ?, ?)", (item_id, uid, role))


def _set_requirements(conn, item_id: int, req_ids: list[int]) -> None:
    conn.execute("DELETE FROM key_item_requirements WHERE item_id = ?", (item_id,))
    for rid in dict.fromkeys(req_ids):
        if db.one(conn, "SELECT 1 FROM requirements WHERE id = ? AND deleted_at IS NULL", (rid,)):
            conn.execute("INSERT OR IGNORE INTO key_item_requirements(item_id, requirement_id) VALUES (?, ?)", (item_id, rid))


def _log(conn, item_id: int, kind: str, body: str, user_id: int | None) -> int:
    return db.insert(conn, "key_item_updates", {"item_id": item_id, "kind": kind, "body": body, "created_by": user_id, "created_at": db.utcnow()})


async def _save_files(ctx: Ctx, item_id: int, update_id: int | None, uploads) -> list[str]:
    names = []
    d = files_dir(ctx.cfg, item_id)
    d.mkdir(parents=True, exist_ok=True)
    for up in uploads:
        if not isinstance(up, UploadFile) or not up.filename:
            continue
        name = sanitize_filename(up.filename)
        stored = f"{uuid.uuid4().hex}{Path(name).suffix.lower()[:10]}"
        size = await run_in_threadpool(copy_stream_limited, up.file, d / stored, ctx.max_upload_bytes)
        await up.close()
        db.insert(ctx.conn, "key_item_files", {"item_id": item_id, "update_id": update_id, "filename": name, "size_bytes": size, "stored_name": stored, "uploaded_by": ctx.user["id"], "created_at": db.utcnow()})
        names.append(name)
    return names


def _notify_item(ctx: Ctx, item, title: str, body: str, kind: str = "item_update", update_id: int | None = None) -> None:
    notify_users(ctx.conn, ctx.notifier, ctx.base_url, people_ids(ctx.conn, item["id"]), kind, title, body, f"/items/{item['id']}", "item_update" if update_id else "key_item", update_id or item["id"], exclude=ctx.user["id"])


def _due_state(item, now: str) -> str:
    if item["status"] == "done":
        return "done"
    if item["next_remind_at"] and item["next_remind_at"] <= now:
        return "overdue"
    return "ok"


# ---------- 列表 ----------

@router.get("")
async def items_list(ctx: Ctx = Depends(get_ctx), project: str = "", status: str = "open", q: str = "", mine: int = 0):
    ctx.require_user()
    where = ["i.deleted_at IS NULL"]
    params: list = []
    if project in PROJECTS:
        where.append("i.project = ?"); params.append(project)
    if status in STATUS:
        where.append("i.status = ?"); params.append(status)
    if mine:
        where.append("EXISTS (SELECT 1 FROM key_item_people p WHERE p.item_id = i.id AND p.user_id = ?)"); params.append(ctx.user["id"])
    if q.strip():
        like = f"%{q.strip()}%"
        where.append("(i.title LIKE ? OR i.description LIKE ? OR EXISTS (SELECT 1 FROM key_item_people p JOIN users u ON u.id = p.user_id WHERE p.item_id = i.id AND u.name LIKE ?))")
        params += [like, like, like]
    rows = db.all_rows(
        ctx.conn,
        f"""SELECT i.*,
              (SELECT GROUP_CONCAT(u.name, '、') FROM key_item_people p JOIN users u ON u.id = p.user_id WHERE p.item_id = i.id AND p.role = 'owner') AS owner_names,
              (SELECT GROUP_CONCAT(u.name, '、') FROM key_item_people p JOIN users u ON u.id = p.user_id WHERE p.item_id = i.id AND p.role = 'reporter') AS reporter_names,
              (SELECT COUNT(*) FROM key_item_updates x WHERE x.item_id = i.id AND x.kind = 'progress') AS progress_count
            FROM key_items i WHERE {' AND '.join(where)}
            ORDER BY CASE WHEN i.status = 'open' AND i.next_remind_at <= ? THEN 0 ELSE 1 END, i.updated_at DESC LIMIT 300""",
        [*params, db.utcnow()],
    )
    now = db.utcnow()
    return ctx.render("items/list.html", items=[{"i": r, "state": _due_state(r, now), "freq": freq_label(r["frequency"], r["interval_hours"])} for r in rows], project=project, status=status, q=q, mine=mine, FREQUENCIES=FREQUENCIES, STATUS=STATUS)


# ---------- 新建 / 编辑 ----------

def _form_context(ctx: Ctx, item=None):
    return {
        "item": item, "users": queries.active_users(ctx.conn), "FREQUENCIES": FREQUENCIES,
        "owner_ids": [u["id"] for u in people(ctx.conn, item["id"], "owner")] if item else [ctx.user["id"]],
        "reporter_ids": [u["id"] for u in people(ctx.conn, item["id"], "reporter")] if item else [],
        "req_ids": [r["requirement_id"] for r in db.all_rows(ctx.conn, "SELECT requirement_id FROM key_item_requirements WHERE item_id = ?", (item["id"],))] if item else [],
        "requirements": db.all_rows(ctx.conn, "SELECT id, name, project FROM requirements WHERE deleted_at IS NULL AND kind != 'v2' ORDER BY updated_at DESC LIMIT 300"),
        "custom": _custom_fields(item["interval_hours"]) if item and item["frequency"] == "custom" else {"value": "", "unit": "days"},
    }


def _custom_fields(hours: int) -> dict:
    if hours % 24 == 0:
        return {"value": hours // 24, "unit": "days"}
    if 168 % hours == 0:
        return {"value": 168 // hours, "unit": "per_week"}
    return {"value": hours, "unit": "hours"}


@router.get("/new")
async def item_new_page(ctx: Ctx = Depends(get_ctx), project: str = ""):
    ctx.require_user()
    return ctx.render("items/form.html", mode="new", project=project, **_form_context(ctx))


def _read_form(form) -> dict:
    project = str(form.get("project") or "")
    title = str(form.get("title") or "").strip()
    due = str(form.get("due_date") or "").strip()
    if project not in PROJECTS or not title:
        raise HTTPException(400, "请填写项目与标题")
    frequency, hours = parse_interval(form)
    if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        raise HTTPException(400, "截止日期格式应为 YYYY-MM-DD")
    return {"project": project, "title": title[:200], "description": str(form.get("description") or "").strip(), "frequency": frequency, "interval_hours": hours, "due_date": due or None}


@router.post("/new")
async def item_new(request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    data = _read_form(form)
    owners = queries.parse_ids(form, "owners") or [ctx.user["id"]]
    now = db.utcnow()
    item_id = db.insert(ctx.conn, "key_items", {**data, "status": "open", "created_by": ctx.user["id"], "created_at": now, "updated_by": ctx.user["id"], "updated_at": now, "last_progress_at": now, "next_remind_at": next_remind(now, data["interval_hours"])})
    _set_people(ctx.conn, item_id, "owner", owners)
    _set_people(ctx.conn, item_id, "reporter", queries.parse_ids(form, "reporters"))
    _set_requirements(ctx.conn, item_id, queries.parse_ids(form, "requirements"))
    uid = _log(ctx.conn, item_id, "create", "", ctx.user["id"])
    names = await _save_files(ctx, item_id, uid, form.getlist("files"))
    item = item_or_404(ctx.conn, item_id)
    _notify_item(ctx, item, f"【重点事项】{ctx.user['name']} 新建了「{data['title']}」", (data["description"][:200] + (f"\n附件：{'、'.join(names)}" if names else "")).strip(), update_id=uid)
    ctx.flash("ok", "重点事项已创建")
    return ctx.redirect(f"/items/{item_id}")


@router.get("/{item_id}/edit")
async def item_edit_page(item_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    item = item_or_404(ctx.conn, item_id)
    return ctx.render("items/form.html", mode="edit", project=item["project"], **_form_context(ctx, item))


@router.post("/{item_id}/edit")
async def item_edit(item_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    item = item_or_404(ctx.conn, item_id)
    data = _read_form(form)
    changes = []
    if data["interval_hours"] != item["interval_hours"]:
        changes.append(f"提醒频率 {freq_label(item['frequency'], item['interval_hours'])} → {freq_label(data['frequency'], data['interval_hours'])}")
        data["next_remind_at"] = next_remind(item["last_progress_at"], data["interval_hours"])
    if data["title"] != item["title"]:
        changes.append(f"标题改为「{data['title']}」")
    if (data["due_date"] or "") != (item["due_date"] or ""):
        changes.append(f"截止日期 {item['due_date'] or '无'} → {data['due_date'] or '无'}")
    if data["description"] != item["description"]:
        changes.append("修改了描述")
    db.update(ctx.conn, "key_items", item_id, {**data, "updated_by": ctx.user["id"], "updated_at": db.utcnow()})
    _set_people(ctx.conn, item_id, "owner", queries.parse_ids(form, "owners") or [ctx.user["id"]])
    _set_people(ctx.conn, item_id, "reporter", queries.parse_ids(form, "reporters"))
    _set_requirements(ctx.conn, item_id, queries.parse_ids(form, "requirements"))
    uid = _log(ctx.conn, item_id, "edit", "；".join(changes) or "修改了负责人/汇报对象/关联需求", ctx.user["id"])
    item = item_or_404(ctx.conn, item_id)
    _notify_item(ctx, item, f"【重点事项】{ctx.user['name']} 修改了「{item['title']}」", "；".join(changes), update_id=uid)
    ctx.flash("ok", "已保存")
    return ctx.redirect(f"/items/{item_id}")


# ---------- 详情 / 进展 / 完成 ----------

@router.get("/{item_id}")
async def item_detail(item_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    item = item_or_404(ctx.conn, item_id)
    updates = db.all_rows(ctx.conn, "SELECT x.*, u.name AS author FROM key_item_updates x LEFT JOIN users u ON u.id = x.created_by WHERE x.item_id = ? ORDER BY x.id DESC", (item_id,))
    files = db.all_rows(ctx.conn, "SELECT f.*, u.name AS uploader FROM key_item_files f LEFT JOIN users u ON u.id = f.uploaded_by WHERE f.item_id = ? AND f.deleted_at IS NULL ORDER BY f.id", (item_id,))
    files_by_update: dict = {}
    for f in files:
        files_by_update.setdefault(f["update_id"], []).append(f)
    reads = {u["id"]: read_status(ctx.conn, "item_update", u["id"]) for u in updates if u["kind"] in ("progress", "complete", "reopen", "edit", "create")}
    reqs = db.all_rows(ctx.conn, "SELECT r.id, r.name, r.project FROM key_item_requirements k JOIN requirements r ON r.id = k.requirement_id WHERE k.item_id = ? AND r.deleted_at IS NULL", (item_id,))
    return ctx.render(
        "items/detail.html", item=item, owners=people(ctx.conn, item_id, "owner"), reporters=people(ctx.conn, item_id, "reporter"), updates=updates, files_by_update=files_by_update,
        reads=reads, reqs=reqs, FREQUENCIES=FREQUENCIES, STATUS=STATUS, UPDATE_KINDS=UPDATE_KINDS, state=_due_state(item, db.utcnow()), freq=freq_label(item["frequency"], item["interval_hours"]),
        can_delete=ctx.is_admin or item["created_by"] == ctx.user["id"], creator=queries.user_name(ctx.conn, item["created_by"]),
    )


@router.post("/{item_id}/progress")
async def item_progress(item_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    item = item_or_404(ctx.conn, item_id)
    body = str(form.get("body") or "").strip()
    uploads = [u for u in form.getlist("files") if isinstance(u, UploadFile) and u.filename]
    if not body and not uploads:
        ctx.flash("err", "请填写进展内容或附上文件")
        return ctx.redirect(f"/items/{item_id}")
    uid = _log(ctx.conn, item_id, "progress", body, ctx.user["id"])
    names = await _save_files(ctx, item_id, uid, uploads)
    now = db.utcnow()
    db.update(ctx.conn, "key_items", item_id, {"last_progress_at": now, "next_remind_at": next_remind(now, item["interval_hours"]), "updated_by": ctx.user["id"], "updated_at": now})
    _notify_item(ctx, item, f"【重点事项】{ctx.user['name']} 更新了「{item['title']}」的进展", (body[:300] + (f"\n附件：{'、'.join(names)}" if names else "")).strip(), update_id=uid)
    ctx.flash("ok", "进展已记录，提醒周期已重置")
    return ctx.redirect(f"/items/{item_id}")


@router.post("/{item_id}/status")
async def item_status(item_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    item = item_or_404(ctx.conn, item_id)
    done = str(form.get("status")) == "done"
    now = db.utcnow()
    if done:
        db.update(ctx.conn, "key_items", item_id, {"status": "done", "completed_at": now, "completed_by": ctx.user["id"], "next_remind_at": None, "updated_by": ctx.user["id"], "updated_at": now})
        uid = _log(ctx.conn, item_id, "complete", str(form.get("body") or "").strip(), ctx.user["id"])
        _notify_item(ctx, item, f"【重点事项】{ctx.user['name']} 标记「{item['title']}」已完成", str(form.get("body") or "").strip()[:300], update_id=uid)
        ctx.flash("ok", "已标记完成")
    else:
        db.update(ctx.conn, "key_items", item_id, {"status": "open", "completed_at": None, "completed_by": None, "last_progress_at": now, "next_remind_at": next_remind(now, item["interval_hours"]), "updated_by": ctx.user["id"], "updated_at": now})
        uid = _log(ctx.conn, item_id, "reopen", "", ctx.user["id"])
        _notify_item(ctx, item, f"【重点事项】{ctx.user['name']} 重新打开了「{item['title']}」", "", update_id=uid)
        ctx.flash("ok", "已重新打开")
    return ctx.redirect(f"/items/{item_id}")


@router.post("/{item_id}/delete")
async def item_delete(item_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    item = item_or_404(ctx.conn, item_id)
    if not (ctx.is_admin or item["created_by"] == ctx.user["id"]):
        raise HTTPException(403, "只有创建人或管理员可以删除")
    db.update(ctx.conn, "key_items", item_id, {"deleted_at": db.utcnow(), "deleted_by": ctx.user["id"]})
    db.audit(ctx.conn, ctx.user, "delete_key_item", "key_item", item_id, {"title": item["title"]})
    ctx.flash("ok", f"重点事项「{item['title']}」已删除")
    return ctx.redirect("/items")


@router.get("/{item_id}/files/{file_id}")
async def item_file(item_id: int, file_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    item_or_404(ctx.conn, item_id)
    f = db.one(ctx.conn, "SELECT * FROM key_item_files WHERE id = ? AND item_id = ? AND deleted_at IS NULL", (file_id, item_id))
    if not f:
        raise HTTPException(404, "附件不存在")
    path = files_dir(ctx.cfg, item_id) / f["stored_name"]
    if not path.is_file():
        raise HTTPException(404, "附件文件缺失")
    return FileResponse(str(path), filename=f["filename"])


@router.post("/{item_id}/files/{file_id}/delete")
async def item_file_delete(item_id: int, file_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ctx.require_user()
    form = await request.form()
    ctx.check_csrf(form)
    item = item_or_404(ctx.conn, item_id)
    f = db.one(ctx.conn, "SELECT * FROM key_item_files WHERE id = ? AND item_id = ? AND deleted_at IS NULL", (file_id, item_id))
    if not f:
        raise HTTPException(404, "附件不存在")
    if not (ctx.is_admin or f["uploaded_by"] == ctx.user["id"] or item["created_by"] == ctx.user["id"]):
        raise HTTPException(403, "只能删除自己上传的附件")
    db.update(ctx.conn, "key_item_files", file_id, {"deleted_at": db.utcnow()})
    ctx.flash("ok", "附件已删除")
    return ctx.redirect(f"/items/{item_id}")


# ---------- 定时提醒（后台循环调用） ----------

def run_reminders(cfg: Config, conn: sqlite3.Connection, notifier: Notifier, base_url: str, now: str | None = None) -> int:
    """到期未更新的进行中事项：提醒负责人与汇报对象，并把下次提醒推一个周期。返回提醒的事项数。"""
    now = now or db.utcnow()
    due = db.all_rows(conn, "SELECT * FROM key_items WHERE deleted_at IS NULL AND status = 'open' AND next_remind_at IS NOT NULL AND next_remind_at <= ?", (now,))
    n = 0
    for item in due:
        last = db.parse_utc(item["last_progress_at"])
        days = max(0, (db.parse_utc(now) - last).days) if last else 0
        owners = [u["name"] for u in people(conn, item["id"], "owner")]
        title = f"【重点事项提醒】「{item['title']}」已 {days} 天没有进展更新"
        hours = max(1, int(round((db.parse_utc(now) - last).total_seconds() / 3600))) if last else 0
        if days == 0:
            title = f"【重点事项提醒】「{item['title']}」已 {hours} 小时没有进展更新"
        body = f"负责人：{'、'.join(owners) or '—'}；提醒频率：{freq_label(item['frequency'], item['interval_hours'])}" + (f"；截止 {item['due_date']}" if item["due_date"] else "") + "。负责人更新进展后计时会重置。"
        uid = _log(conn, item["id"], "remind", f"系统提醒：已 {days} 天未更新", None)
        notify_users(conn, notifier, base_url, people_ids(conn, item["id"]), "item_remind", title, body, f"/items/{item['id']}", "item_update", uid)
        db.update(conn, "key_items", item["id"], {"next_remind_at": next_remind(now, item["interval_hours"])})
        n += 1
    return n
