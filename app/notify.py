"""通知中心 + Telegram 推送。

每条通知先落库（站内可见、可标已读），再按用户偏好与 Bot 关注状态推送到 Telegram，消息带「已读」按钮（callback → webhook）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import urllib.error
import urllib.request
from typing import Any

from . import db

log = logging.getLogger("dcpm.notify")

# 通知类型（用户可在「我的提醒」逐项开关，默认全开）
KINDS: dict[str, str] = {
    "doc_version": "我负责 / 创建 / 上传过的文档有新版本",
    "doc_comment": "我负责 / 创建 / 上传过的文档收到留言",
    "fav_version": "我收藏的需求有新版本",
    "fav_comment": "我收藏的需求收到留言",
    "item_update": "我负责或被汇报的重点事项有新进展 / 完成 / 变更",
    "item_remind": "重点事项超期未更新的定时提醒",
    "item_nudge": "有人催办我负责的重点事项",
    "system": "系统消息（测试消息、绑定提醒等）",
}


class Notifier:
    def __init__(self, bot_token: str, dry_run: bool = False):
        self.bot_token = bot_token
        self.dry_run = dry_run
        self.sent: list[tuple[int, str]] = []          # dry_run：记录 (tg_id, text)
        self.started: set[int] = set()                 # dry_run：模拟已关注 Bot 的 tg_id
        self._seq = 0

    # ---- Telegram API ----
    def _call(self, method: str, payload: dict) -> dict | None:
        if not self.bot_token:
            return None
        data = json.dumps(payload).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.bot_token}/{method}", data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except Exception:  # noqa: BLE001
                body = {"description": str(e)}
            log.info("telegram %s 失败 code=%s desc=%s", method, e.code, body.get("description"))
            return {"ok": False, "error_code": e.code, "description": body.get("description", "")}
        except Exception as e:  # noqa: BLE001
            log.warning("telegram %s 异常 %s", method, e)
            return None

    def send(self, tg_id: int | None, text: str, buttons: list[dict] | None = None) -> int | None:
        """发送消息，返回 message_id；失败返回 None。"""
        if not tg_id or not self.bot_token:
            return None
        if self.dry_run:
            self.sent.append((int(tg_id), text))
            self._seq += 1
            return self._seq if (not self.started or int(tg_id) in self.started) else None
        payload: dict[str, Any] = {"chat_id": int(tg_id), "text": text, "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        res = self._call("sendMessage", payload)
        if res and res.get("ok"):
            return res["result"]["message_id"]
        return None

    def webhook_secret(self) -> str:
        return hashlib.sha256(("webhook:" + self.bot_token).encode()).hexdigest()[:48]

    def set_webhook(self, url: str) -> bool:
        if self.dry_run or not self.bot_token:
            return False
        res = self._call("setWebhook", {"url": url, "secret_token": self.webhook_secret(), "allowed_updates": ["message", "callback_query", "my_chat_member"], "drop_pending_updates": False})
        ok = bool(res and res.get("ok"))
        log.info("setWebhook %s -> %s", url, res)
        return ok

    def chat_exists(self, tg_id: int) -> bool | None:
        """getChat：用户和 Bot 有过对话返回 True；'chat not found' 返回 False；网络错误 None。"""
        if self.dry_run:
            return int(tg_id) in self.started
        res = self._call("getChat", {"chat_id": int(tg_id)})
        if res is None:
            return None
        if res.get("ok"):
            return True
        return False if res.get("error_code") in (400, 403) else None

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        if not self.dry_run:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def mark_message_read(self, tg_id: int, message_id: int) -> None:
        if not self.dry_run:
            self._call("editMessageReplyMarkup", {"chat_id": int(tg_id), "message_id": int(message_id), "reply_markup": {"inline_keyboard": [[{"text": "✓ 已读", "callback_data": "noop"}]]}})


# ---------- 偏好 / 关注状态 ----------

def pref_enabled(conn: sqlite3.Connection, user_id: int, kind: str) -> bool:
    row = db.one(conn, "SELECT enabled FROM notification_prefs WHERE user_id = ? AND kind = ?", (user_id, kind))
    return True if row is None else bool(row["enabled"])


def user_prefs(conn: sqlite3.Connection, user_id: int) -> dict[str, bool]:
    rows = {r["kind"]: bool(r["enabled"]) for r in db.all_rows(conn, "SELECT kind, enabled FROM notification_prefs WHERE user_id = ?", (user_id,))}
    return {k: rows.get(k, True) for k in KINDS}


def set_prefs(conn: sqlite3.Connection, user_id: int, enabled: dict[str, bool]) -> None:
    for kind in KINDS:
        conn.execute(
            "INSERT INTO notification_prefs(user_id, kind, enabled) VALUES (?, ?, ?) ON CONFLICT(user_id, kind) DO UPDATE SET enabled = excluded.enabled",
            (user_id, kind, 1 if enabled.get(kind, True) else 0),
        )


def bot_following(user) -> bool | None:
    """True 已关注；False 明确未关注/已拉黑；None 未知。"""
    if not user or user["tg_id"] is None:
        return False
    if user["bot_blocked_at"] and (not user["bot_started_at"] or user["bot_blocked_at"] >= user["bot_started_at"]):
        return False
    if user["bot_started_at"]:
        return True
    return None


def mark_bot_started(conn: sqlite3.Connection, tg_id: int) -> int | None:
    u = db.one(conn, "SELECT id FROM users WHERE tg_id = ? AND deleted_at IS NULL", (tg_id,))
    if not u:
        return None
    conn.execute("UPDATE users SET bot_started_at = ?, bot_blocked_at = NULL, bot_checked_at = ? WHERE id = ?", (db.utcnow(), db.utcnow(), u["id"]))
    return u["id"]


def mark_bot_blocked(conn: sqlite3.Connection, tg_id: int) -> None:
    conn.execute("UPDATE users SET bot_blocked_at = ?, bot_checked_at = ? WHERE tg_id = ?", (db.utcnow(), db.utcnow(), tg_id))


# ---------- 发通知 ----------

def notify_user(conn: sqlite3.Connection, notifier: Notifier, base_url: str, user_id: int, kind: str, title: str, body: str = "", url: str = "", ref_type: str | None = None, ref_id: int | None = None) -> int | None:
    """落库 + 推送。返回通知 id；用户关闭了该类型则不落库返回 None。"""
    if kind not in KINDS or not pref_enabled(conn, user_id, kind):
        return None
    user = db.one(conn, "SELECT * FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,))
    if not user:
        return None
    nid = db.insert(conn, "notifications", {"user_id": user_id, "kind": kind, "title": title, "body": body, "url": url, "ref_type": ref_type, "ref_id": ref_id, "created_at": db.utcnow()})
    if user["tg_id"] and bot_following(user) is not False:
        text = title + (f"\n{body}" if body else "")
        full_url = (base_url + url) if url.startswith("/") else url
        buttons = [{"text": "已读", "callback_data": f"read:{nid}"}]
        if full_url:
            buttons.append({"text": "打开", "url": full_url})

        def _send():
            mid = notifier.send(user["tg_id"], text, buttons)
            if mid:
                try:
                    c2 = db.connect(conn_path) if conn_path else None
                except Exception:  # noqa: BLE001
                    c2 = None
                target = c2 or conn
                target.execute("UPDATE notifications SET tg_message_id = ?, tg_sent_at = ? WHERE id = ?", (mid, db.utcnow(), nid))
                if c2:
                    c2.close()

        conn_path = _db_path_of(conn)
        if notifier.dry_run:
            _send()
        else:
            threading.Thread(target=_send, daemon=True).start()
    return nid


def _db_path_of(conn: sqlite3.Connection) -> str | None:
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return row[2]
    except Exception:  # noqa: BLE001
        pass
    return None


def notify_users(conn, notifier, base_url, user_ids, kind, title, body="", url="", ref_type=None, ref_id=None, exclude: int | None = None) -> int:
    n = 0
    for uid in dict.fromkeys(user_ids):
        if uid == exclude or not uid:
            continue
        if notify_user(conn, notifier, base_url, uid, kind, title, body, url, ref_type, ref_id):
            n += 1
    return n


# ---------- 文档相关的收件人 ----------

def doc_stakeholders(conn: sqlite3.Connection, req_id: int, doc_id: int | None = None) -> list[int]:
    """需求负责人 + 创建人 + 该文档（或该需求所有文档）的历史上传人。"""
    ids = [r["id"] for r in conn.execute(
        """SELECT DISTINCT u.id FROM users u WHERE u.deleted_at IS NULL AND (
               u.id IN (SELECT user_id FROM requirement_owners WHERE requirement_id = ?)
               OR u.id = (SELECT created_by FROM requirements WHERE id = ?))""", (req_id, req_id))]
    if doc_id:
        ids += [r["uploaded_by"] for r in conn.execute("SELECT DISTINCT uploaded_by FROM versions WHERE document_id = ? AND deleted_at IS NULL", (doc_id,))]
    else:
        ids += [r["uploaded_by"] for r in conn.execute("SELECT DISTINCT v.uploaded_by FROM versions v JOIN documents d ON d.id = v.document_id WHERE d.requirement_id = ? AND v.deleted_at IS NULL", (req_id,))]
    return [i for i in dict.fromkeys(ids) if i]


def fav_users(conn: sqlite3.Connection, req_id: int) -> list[int]:
    return [r["user_id"] for r in conn.execute("SELECT user_id FROM favorites WHERE requirement_id = ?", (req_id,))]


def notify_doc_event(conn, notifier, base_url, req, doc, event: str, title: str, body: str, url: str, actor_id: int | None) -> None:
    """文档新版本 / 留言：干系人走 doc_*，收藏者走 fav_*（同一人只收一条）。"""
    stake = doc_stakeholders(conn, req["id"], doc["id"])
    sent = set()
    for uid in stake:
        if uid != actor_id and notify_user(conn, notifier, base_url, uid, f"doc_{event}", title, body, url, "requirement", req["id"]):
            sent.add(uid)
    for uid in fav_users(conn, req["id"]):
        if uid != actor_id and uid not in sent and uid not in stake:
            notify_user(conn, notifier, base_url, uid, f"fav_{event}", title, body, url, "requirement", req["id"])


# ---------- 已读 ----------

def mark_read(conn: sqlite3.Connection, notifier: Notifier, notif_id: int, user_id: int | None = None) -> sqlite3.Row | None:
    n = db.one(conn, "SELECT * FROM notifications WHERE id = ?", (notif_id,))
    if not n or (user_id is not None and n["user_id"] != user_id):
        return None
    if not n["read_at"]:
        conn.execute("UPDATE notifications SET read_at = ? WHERE id = ?", (db.utcnow(), notif_id))
        if n["tg_message_id"]:
            u = db.one(conn, "SELECT tg_id FROM users WHERE id = ?", (n["user_id"],))
            if u and u["tg_id"]:
                threading.Thread(target=notifier.mark_message_read, args=(u["tg_id"], n["tg_message_id"]), daemon=True).start() if not notifier.dry_run else None
    return n


def unread_count(conn: sqlite3.Connection, user_id: int) -> int:
    return db.one(conn, "SELECT COUNT(*) AS c FROM notifications WHERE user_id = ? AND read_at IS NULL", (user_id,))["c"]


def read_status(conn: sqlite3.Connection, ref_type: str, ref_id: int, kind: str | None = None) -> list[dict]:
    """某对象相关通知的已读情况：[{user_id, name, read_at}]（按通知去重到人）。"""
    sql = """SELECT n.user_id, u.name, MAX(n.read_at) AS read_at, MIN(n.read_at IS NULL) AS has_unread
             FROM notifications n JOIN users u ON u.id = n.user_id WHERE n.ref_type = ? AND n.ref_id = ?"""
    params: list = [ref_type, ref_id]
    if kind:
        sql += " AND n.kind = ?"
        params.append(kind)
    sql += " GROUP BY n.user_id ORDER BY u.name"
    return [dict(r) for r in db.all_rows(conn, sql, params)]


# 兼容旧调用
def recipients_for_requirement(conn: sqlite3.Connection, req_id: int, exclude_user_id: int | None = None) -> list[sqlite3.Row]:
    rows = conn.execute(
        """SELECT DISTINCT u.id, u.name, u.tg_id FROM users u
           WHERE u.deleted_at IS NULL AND u.tg_id IS NOT NULL AND (
               u.id IN (SELECT user_id FROM requirement_owners WHERE requirement_id = ?)
               OR u.id = (SELECT created_by FROM requirements WHERE id = ?))""",
        (req_id, req_id),
    ).fetchall()
    return [r for r in rows if r["id"] != exclude_user_id]
