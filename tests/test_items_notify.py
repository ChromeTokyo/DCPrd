"""通知中心 / 我的提醒 / Telegram Webhook / Bot 关注检测 / 重点事项与定时提醒。"""
import datetime as dt
import re

from app import db
from app.routes.items import run_reminders
from tests.conftest import create_single, csrf_of, html_file, login, share_code_of
from tests.test_extras import invite


def uid_of(admin, name):
    return int(re.search(rf'{name}</span>.*?/admin/users/(\d+)/', admin.get("/admin/users").text, re.S).group(1))


def test_notification_center_and_prefs(app, superuser, client_factory):
    csrf = csrf_of(superuser)
    notifier = app.state.notifier
    alice = invite(superuser, client_factory, "Alice", 8001)
    alice_id = uid_of(superuser, "Alice")
    rid = create_single(superuser, csrf, "通知需求", owners=alice_id, file=html_file("a.html", "1"))
    doc_id = int(re.search(r"/doc/(\d+)/upload", superuser.get(f"/req/{rid}").text).group(1))
    alice.post("/notifications/read-all", data={"csrf": csrf_of(alice)}, follow_redirects=False)  # 清掉 v1 的通知
    notifier.sent.clear()
    superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf, "note": "v2"}, files={"file": html_file("b.html", "2")}, follow_redirects=False)
    # Alice 收到站内通知 + Telegram（dry-run 记录），铃铛显示未读 1
    home = alice.get("/").text
    assert 'class="bell has"' in home and '<span class="badge">1</span>' in home
    assert [t for t, _ in notifier.sent] == [8001]
    page = alice.get("/notifications").text
    assert "上传了「通知需求」v2" in page and "已推送 Telegram" in page
    nid = int(re.search(r"/notifications/(\d+)/go", page).group(1))
    # 点击 → 已读 + 跳转
    r = alice.get(f"/notifications/{nid}/go", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/req/{rid}"
    assert 'class="bell "' in alice.get("/").text or 'class="bell "' in alice.get("/").text.replace('class="bell has"', "")
    assert "没有未读通知" in alice.get("/notifications").text and "已读 " in alice.get("/notifications?all=1").text
    # 别人不能读我的通知
    assert superuser.get(f"/notifications/{nid}/go", follow_redirects=False).status_code == 404
    # 关闭 doc_version 后不再产生通知（也不推送）
    acsrf = csrf_of(alice)
    prefs = alice.get("/me/notifications").text
    assert 'name="kind_doc_version" value="1" checked' in prefs
    alice.post("/me/notifications", data={"csrf": acsrf, "kind_doc_comment": "1", "kind_item_update": "1"}, follow_redirects=False)
    assert 'name="kind_doc_version" value="1" checked' not in alice.get("/me/notifications").text
    notifier.sent.clear()
    superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf}, files={"file": html_file("c.html", "3")}, follow_redirects=False)
    assert notifier.sent == [] and "没有未读通知" in alice.get("/notifications").text
    # 留言仍然通知（doc_comment 开着）
    code = share_code_of(superuser.get(f"/req/{rid}").text)
    client_factory().post(f"/s/{code}/__comments", data={"author": "路人", "body": "问题"})
    assert "路人 在「通知需求」留言" in alice.get("/notifications").text
    # 收藏者收到 fav_*（Bob 不是干系人）
    bob = invite(superuser, client_factory, "Bob", 8002)
    bob.post(f"/req/{rid}/fav", data={"csrf": csrf_of(bob)}, follow_redirects=False)
    superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf}, files={"file": html_file("d.html", "4")}, follow_redirects=False)
    assert "我收藏的需求有新版本" in bob.get("/notifications").text
    # 全部已读
    bob.post("/notifications/read-all", data={"csrf": csrf_of(bob)}, follow_redirects=False)
    assert "没有未读通知" in bob.get("/notifications").text


def test_webhook_and_bot_follow_status(app, superuser, client_factory):
    csrf = csrf_of(superuser)
    notifier = app.state.notifier
    secret = notifier.webhook_secret()
    alice = invite(superuser, client_factory, "Alice", 8001)
    alice_id = uid_of(superuser, "Alice")
    users = superuser.get("/admin/users").text
    assert "? 未知" in users and "Bot 通知" in users
    # 错误 secret 拒绝
    assert client_factory().post("/tg/webhook", json={"message": {"chat": {"id": 8001, "type": "private"}, "text": "/start"}}).status_code == 403
    # /start → 记录已关注并回复
    notifier.sent.clear()
    r = client_factory().post("/tg/webhook", json={"update_id": 1, "message": {"chat": {"id": 8001, "type": "private"}, "from": {"id": 8001}, "text": "/start"}}, headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert r.status_code == 200 and notifier.sent and "已连接" in notifier.sent[0][1]
    assert "✓ 已关注" in superuser.get("/admin/users").text
    assert "✓ 已关注" in alice.get("/me/notifications").text
    # 未绑定的 tg 发 /start → 提示先绑定
    notifier.sent.clear()
    client_factory().post("/tg/webhook", json={"update_id": 2, "message": {"chat": {"id": 9999, "type": "private"}, "text": "/start"}}, headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert "尚未绑定" in notifier.sent[0][1]
    # 拉黑 → 未关注；再 Start → 已关注
    client_factory().post("/tg/webhook", json={"update_id": 3, "my_chat_member": {"chat": {"id": 8001, "type": "private"}, "new_chat_member": {"status": "kicked"}}}, headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert "✗ 未关注" in superuser.get("/admin/users").text
    # 未关注时不推送 Telegram，但站内仍有通知
    notifier.sent.clear()
    rid = create_single(superuser, csrf, "N", owners=alice_id, file=html_file("a.html", "1"))
    assert notifier.sent == [] and "上传了「N」v1" in alice.get("/notifications").text  # 站内有，Telegram 不推
    doc_id = int(re.search(r"/doc/(\d+)/upload", superuser.get(f"/req/{rid}").text).group(1))
    alice.post("/notifications/read-all", data={"csrf": csrf_of(alice)}, follow_redirects=False)
    superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf}, files={"file": html_file("b.html", "2")}, follow_redirects=False)
    assert notifier.sent == [] and "上传了「N」v2" in alice.get("/notifications").text
    # 管理员批量检测（dry-run：notifier.started 模拟 getChat）
    notifier.started.add(8001)
    r = superuser.post("/admin/users/bot-check", data={"csrf": csrf}, follow_redirects=True)
    assert "已关注 2" in r.text or "已关注 1" in r.text  # super(1001) 不在 started → 未关注
    assert "✓ 已关注" in superuser.get("/admin/users").text
    # callback 已读：Telegram 按钮 → 站内已读
    nid = int(re.search(r"/notifications/(\d+)/go", alice.get("/notifications").text).group(1))
    r = client_factory().post("/tg/webhook", json={"update_id": 4, "callback_query": {"id": "cb1", "from": {"id": 8001}, "data": f"read:{nid}"}}, headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert r.status_code == 200 and "没有未读通知" in alice.get("/notifications").text
    # 别人的 tg 不能替我已读
    superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf}, files={"file": html_file("c.html", "3")}, follow_redirects=False)
    nid2 = int(re.search(r"/notifications/(\d+)/go", alice.get("/notifications").text).group(1))
    client_factory().post("/tg/webhook", json={"update_id": 5, "callback_query": {"id": "cb2", "from": {"id": 1001}, "data": f"read:{nid2}"}}, headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert "没有未读通知" not in alice.get("/notifications").text
    # 本人检测
    notifier.started.discard(8001)
    r = alice.post("/me/bot-check", data={"csrf": csrf_of(alice)}, follow_redirects=True)
    assert "还没有和 Bot 建立对话" in r.text


def test_key_items_flow_and_reminders(app, cfg, superuser, client_factory):
    csrf = csrf_of(superuser)
    notifier = app.state.notifier
    notifier.started.clear()
    owner = invite(superuser, client_factory, "负责人", 8101)
    boss = invite(superuser, client_factory, "老板", 8102)
    owner_id, boss_id = uid_of(superuser, "负责人"), uid_of(superuser, "老板")
    rid = create_single(superuser, csrf, "关联需求")
    # 新建：项目、标题、描述、附件、负责人多选、汇报对象多选、频率、截止
    notifier.sent.clear()
    r = superuser.post("/items/new", data={"csrf": csrf, "project": "eb", "title": "Q4 支付改版", "description": "目标：…", "frequency": "weekly", "due_date": "2026-12-31", "owners": [owner_id], "reporters": [boss_id], "requirements": [rid]},
                       files=[("files", ("plan.md", b"# plan", "text/markdown")), ("files", ("x.png", b"\x89PNG", "image/png"))], follow_redirects=False)
    assert r.status_code == 303
    item_id = int(re.search(r"/items/(\d+)", r.headers["location"]).group(1))
    detail = superuser.get(f"/items/{item_id}").text
    assert "Q4 支付改版" in detail and "负责人" in detail and "老板" in detail and "每周" in detail and "2026-12-31" in detail and "每周 2 次" in superuser.get("/items/new").text and "plan.md" in detail and "x.png" in detail and "关联需求" in detail
    assert sorted(t for t, _ in notifier.sent) == [8101, 8102] and "新建了「Q4 支付改版」" in notifier.sent[0][1]
    # 已读状态：两人未读
    assert "未读：" in detail and detail.count('class="rd no"') == 2
    # 汇报对象也能写进展；进展重置周期；通知负责人（排除操作者）
    notifier.sent.clear()
    before = db.one(db.connect(cfg.db_path), "SELECT next_remind_at, last_progress_at FROM key_items WHERE id = ?", (item_id,))
    r = boss.post(f"/items/{item_id}/progress", data={"csrf": csrf_of(boss), "body": "本周完成方案评审"}, files={"files": ("minutes.md", b"# m", "text/markdown")}, follow_redirects=True)
    assert "进展已记录" in r.text and "本周完成方案评审" in r.text and "minutes.md" in r.text
    assert [t for t, _ in notifier.sent] == [8101] and "更新了「Q4 支付改版」的进展" in notifier.sent[0][1]
    # 负责人读通知 → 详情页显示已读
    owner.get("/notifications/%s/go" % re.search(r"/notifications/(\d+)/go", owner.get("/notifications").text).group(1), follow_redirects=False)
    d = boss.get(f"/items/{item_id}").text
    assert re.search(r'已读：</span><span class="rd ok"[^>]*>负责人', d)
    # 附件下载
    fid = int(re.search(rf"/items/{item_id}/files/(\d+)", d).group(1))
    assert superuser.get(f"/items/{item_id}/files/{fid}").status_code == 200
    # 列表 / 筛选 / 与我相关
    lst = owner.get("/items").text
    assert "Q4 支付改版" in lst and "进行中" in lst and "1 条进展" in lst
    assert "Q4 支付改版" not in owner.get("/items?project=im").text
    assert "Q4 支付改版" in owner.get("/items?mine=1").text and "Q4 支付改版" not in superuser.get("/items?mine=1").text
    assert "Q4 支付改版" in superuser.get("/items?q=老板").text
    # 定时提醒：未到期不提醒；把时间拨到 8 天后 → 提醒负责人与汇报对象，且下次提醒再推一周
    conn = db.connect(cfg.db_path)
    now = db.utcnow()
    assert run_reminders(cfg, conn, notifier, "http://t", now) == 0
    notifier.sent.clear()
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%S")
    assert run_reminders(cfg, conn, notifier, "http://t", future) == 1
    assert sorted(t for t, _ in notifier.sent) == [8101, 8102] and "已 8 天没有进展更新" in notifier.sent[0][1]
    nxt = db.one(conn, "SELECT next_remind_at FROM key_items WHERE id = ?", (item_id,))["next_remind_at"]
    assert nxt > future
    assert run_reminders(cfg, conn, notifier, "http://t", future) == 0  # 同一时刻不会重复
    assert "系统提醒" in superuser.get(f"/items/{item_id}").text
    conn.execute("UPDATE key_items SET next_remind_at = '2000-01-01T00:00:00' WHERE id = ?", (item_id,))
    assert "超期未更新" in superuser.get("/items").text and "超期未更新" in superuser.get(f"/items/{item_id}").text
    conn.execute("UPDATE key_items SET next_remind_at = ? WHERE id = ?", (nxt, item_id))
    # 汇报对象关掉 item_remind 后不再收到提醒
    boss.post("/me/notifications", data={"csrf": csrf_of(boss), **{f"kind_{k}": "1" for k in ("doc_version", "doc_comment", "fav_version", "fav_comment", "item_update", "system")}}, follow_redirects=False)
    notifier.sent.clear()
    far = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    run_reminders(cfg, conn, notifier, "http://t", far)
    assert [t for t, _ in notifier.sent] == [8101]
    # 编辑：改频率 → 重新计算；标记完成 → 不再提醒；重新打开 → 恢复
    superuser.post(f"/items/{item_id}/edit", data={"csrf": csrf, "project": "eb", "title": "Q4 支付改版（二期）", "frequency": "daily", "owners": [owner_id], "reporters": [boss_id]}, follow_redirects=False)
    d = superuser.get(f"/items/{item_id}").text
    assert "每日" in d and "提醒频率 每周 → 每日" in d and "Q4 支付改版（二期）" in d
    notifier.sent.clear()
    superuser.post(f"/items/{item_id}/status", data={"csrf": csrf, "status": "done"}, follow_redirects=False)
    assert "已完成" in superuser.get(f"/items/{item_id}").text and any("已完成" in t for _, t in notifier.sent)
    assert run_reminders(cfg, conn, notifier, "http://t", far) == 0
    assert "Q4 支付改版" not in superuser.get("/items").text and "Q4 支付改版" in superuser.get("/items?status=done").text
    superuser.post(f"/items/{item_id}/status", data={"csrf": csrf, "status": "open"}, follow_redirects=False)
    assert "进行中" in superuser.get(f"/items/{item_id}").text
    # 权限：任何人可编辑/进展，删除仅创建人或管理员
    assert owner.post(f"/items/{item_id}/delete", data={"csrf": csrf_of(owner)}, follow_redirects=False).status_code == 403
    assert superuser.post(f"/items/{item_id}/delete", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert superuser.get(f"/items/{item_id}").status_code == 404
    conn.close()


def test_custom_intervals_and_member_edit(cfg, superuser, client_factory):
    from app.routes.items import freq_label, parse_interval
    assert freq_label("weekly", 168) == "每周" and freq_label("twice_weekly", 84) == "每周 2 次" and freq_label("custom", 72) == "每 3 天"
    assert freq_label("custom", 56) == "每周 3 次" and freq_label("custom", 12) == "每 12 小时" and freq_label("custom", 48) == "每 2 天"
    csrf = csrf_of(superuser)
    # 自定义：每周 3 次 → 56 小时
    r = superuser.post("/items/new", data={"csrf": csrf, "project": "eb", "title": "自定义间隔", "frequency": "custom", "custom_value": "3", "custom_unit": "per_week"}, follow_redirects=False)
    item_id = int(re.search(r"/items/(\d+)", r.headers["location"]).group(1))
    conn = db.connect(cfg.db_path)
    row = db.one(conn, "SELECT frequency, interval_hours, last_progress_at, next_remind_at FROM key_items WHERE id = ?", (item_id,))
    assert row["frequency"] == "custom" and row["interval_hours"] == 56
    assert (db.parse_utc(row["next_remind_at"]) - db.parse_utc(row["last_progress_at"])) == dt.timedelta(hours=56)
    d = superuser.get(f"/items/{item_id}").text
    assert "每周 3 次" in d
    edit = superuser.get(f"/items/{item_id}/edit").text
    assert 'name="custom_value" value="3"' in edit and '<option value="per_week" selected' in edit
    # 每 12 小时；非法值
    superuser.post(f"/items/{item_id}/edit", data={"csrf": csrf, "project": "eb", "title": "自定义间隔", "frequency": "custom", "custom_value": "12", "custom_unit": "hours"}, follow_redirects=False)
    assert "每 12 小时" in superuser.get(f"/items/{item_id}").text and "提醒频率 每周 3 次 → 每 12 小时" in superuser.get(f"/items/{item_id}").text
    assert superuser.post(f"/items/{item_id}/edit", data={"csrf": csrf, "project": "eb", "title": "x", "frequency": "custom", "custom_value": "0", "custom_unit": "days"}, follow_redirects=False).status_code == 400
    # 预设 每 2 天
    superuser.post(f"/items/{item_id}/edit", data={"csrf": csrf, "project": "eb", "title": "自定义间隔", "frequency": "every2days"}, follow_redirects=False)
    assert db.one(conn, "SELECT interval_hours FROM key_items WHERE id = ?", (item_id,))["interval_hours"] == 48 and "每 2 天" in superuser.get("/items").text
    # 成员信息：管理员改名+备注；成员改自己名
    alice = invite(superuser, client_factory, "Alice", 8301)
    aid = uid_of(superuser, "Alice")
    assert superuser.post(f"/admin/users/{aid}/edit", data={"csrf": csrf, "name": "爱丽丝", "note": "EB 产品经理"}, follow_redirects=False).status_code == 303
    users = superuser.get("/admin/users").text
    assert "爱丽丝" in users and "EB 产品经理" in users and "edit_user" in superuser.get("/admin/audit").text
    assert "爱丽丝" in alice.get("/").text
    assert alice.post(f"/admin/users/{aid}/edit", data={"csrf": csrf_of(alice), "name": "x"}, follow_redirects=False).status_code == 403
    assert alice.post("/me/profile", data={"csrf": csrf_of(alice), "name": "Alice Wang"}, follow_redirects=False).status_code == 303
    assert "Alice Wang" in alice.get("/").text and "EB 产品经理" in alice.get("/me/notifications").text
    assert superuser.post(f"/admin/users/{aid}/edit", data={"csrf": csrf, "name": ""}, follow_redirects=True).text.find("名称不能为空") > 0
    conn.close()
