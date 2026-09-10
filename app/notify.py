"""Telegram 通知：给关注过 Bot 的用户发消息。失败（未关注、被拉黑）静默忽略。"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import urllib.error
import urllib.request

log = logging.getLogger("dcpm.notify")


class Notifier:
    def __init__(self, bot_token: str, dry_run: bool = False):
        self.bot_token = bot_token
        self.dry_run = dry_run
        self.sent: list[tuple[int, str]] = []  # dry_run 模式下记录

    def send(self, tg_id: int | None, text: str) -> bool:
        if not tg_id or not self.bot_token:
            return False
        if self.dry_run:
            self.sent.append((int(tg_id), text))
            return True
        data = json.dumps({"chat_id": int(tg_id), "text": text, "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.bot_token}/sendMessage", data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            log.info("telegram sendMessage 失败 chat=%s code=%s", tg_id, e.code)
        except Exception as e:  # noqa: BLE001
            log.warning("telegram sendMessage 异常 chat=%s %s", tg_id, e)
        return False

    def send_async(self, tg_id: int | None, text: str) -> None:
        if self.dry_run:
            self.send(tg_id, text)
            return
        threading.Thread(target=self.send, args=(tg_id, text), daemon=True).start()


def recipients_for_requirement(conn: sqlite3.Connection, req_id: int, exclude_user_id: int | None = None) -> list[sqlite3.Row]:
    """需求负责人 + 创建人（去重、已绑定 Telegram、未删除）。"""
    rows = conn.execute(
        """SELECT DISTINCT u.id, u.name, u.tg_id FROM users u
           WHERE u.deleted_at IS NULL AND u.tg_id IS NOT NULL AND (
               u.id IN (SELECT user_id FROM requirement_owners WHERE requirement_id = ?)
               OR u.id = (SELECT created_by FROM requirements WHERE id = ?))""",
        (req_id, req_id),
    ).fetchall()
    return [r for r in rows if r["id"] != exclude_user_id]


def notify_requirement(conn: sqlite3.Connection, notifier: Notifier, req_id: int, text: str, exclude_user_id: int | None = None) -> int:
    n = 0
    for u in recipients_for_requirement(conn, req_id, exclude_user_id):
        notifier.send_async(u["tg_id"], text)
        n += 1
    return n
