import time

from app.security import sign_telegram_auth, verify_telegram_auth
from tests.conftest import BOT_TOKEN, csrf_of, login, tg_params


def test_verify_valid_tampered_expired():
    data = {"id": "1", "first_name": "A", "auth_date": str(int(time.time()))}
    data["hash"] = sign_telegram_auth(BOT_TOKEN, data)
    assert verify_telegram_auth(BOT_TOKEN, data)[0]
    bad = {**data, "first_name": "B"}
    ok, reason = verify_telegram_auth(BOT_TOKEN, bad)
    assert not ok and "签名" in reason
    old = {"id": "1", "first_name": "A", "auth_date": str(int(time.time()) - 90000)}
    old["hash"] = sign_telegram_auth(BOT_TOKEN, old)
    ok, reason = verify_telegram_auth(BOT_TOKEN, old)
    assert not ok and "过期" in reason
    assert not verify_telegram_auth("", data)[0]
    assert not verify_telegram_auth(BOT_TOKEN, {"id": "1"})[0]


def test_init_super_then_uninvited_rejected(client, client_factory):
    r = client.get("/login")
    assert "系统初始化" in r.text
    r = login(client, 1001, "First")
    assert r.status_code == 302 and r.headers["location"] == "/"
    home = client.get("/")
    assert "超管" in home.text and "用户管理" in home.text
    assert "系统初始化" not in client_factory().get("/login").text
    other = client_factory()
    r = login(other, 2002, "Second")
    assert r.status_code == 403 and "尚未被邀请" in r.text
    assert other.get("/", follow_redirects=False).status_code == 302


def test_tampered_hash_rejected(client):
    p = tg_params(1001)
    p["first_name"] = "X"
    r = client.get("/auth/telegram", params=p, follow_redirects=False)
    assert r.status_code == 403
    r = client.get("/auth/telegram", params=tg_params(1001, token="999:WRONG"), follow_redirects=False)
    assert r.status_code == 403


def test_invite_bind_and_duplicate(superuser, client_factory):
    csrf = csrf_of(superuser)
    assert superuser.post("/admin/users/new", data={"csrf": csrf, "name": "张三"}, follow_redirects=False).status_code == 303
    page = superuser.get("/admin/users").text
    import re
    code = re.search(r"/invite/([A-Za-z0-9_\-]+)", page).group(1)
    assert len(code) >= 32
    assert superuser.get("/invite/nonexistent").status_code == 404

    zhang = client_factory()
    r = zhang.get(f"/invite/{code}")
    assert r.status_code == 200 and "张三" in r.text
    r = login(zhang, 2002, "Zhang")
    assert r.status_code == 302
    assert "张三" in zhang.get("/").text
    # 邀请链接失效
    assert zhang.get(f"/invite/{code}").status_code == 404

    # 同一 Telegram 绑第二个用户 → 拒绝
    assert superuser.post("/admin/users/new", data={"csrf": csrf, "name": "李四"}, follow_redirects=False).status_code == 303
    code2 = re.search(r"/invite/([A-Za-z0-9_\-]+)", superuser.get("/admin/users").text).group(1)
    dup = client_factory()
    dup.get(f"/invite/{code2}")
    r = login(dup, 2002, "Zhang")
    assert r.status_code == 409 and "不能重复绑定" in r.text
    # 新账号可以绑
    r = login(dup, 3003, "Li")
    assert r.status_code == 302
    assert "李四" in dup.get("/").text

    # 解绑张三 → 会话失效 → 重新生成的链接可绑新账号
    users_page = superuser.get("/admin/users").text
    zhang_id = re.search(r'张三</td>.*?/admin/users/(\d+)/unbind', users_page, re.S).group(1)
    r = superuser.post(f"/admin/users/{zhang_id}/unbind", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert zhang.get("/", follow_redirects=False).status_code == 302
    code3 = re.search(r'张三</td>.*?/invite/([A-Za-z0-9_\-]+)', superuser.get("/admin/users").text, re.S).group(1)
    assert code3 != code
    fresh = client_factory()
    fresh.get(f"/invite/{code3}")
    assert login(fresh, 4004, "NewZhang").status_code == 302
    assert "张三" in fresh.get("/").text
    assert "unbind_user" in superuser.get("/admin/audit").text


def test_logout_and_csrf(superuser):
    csrf = csrf_of(superuser)
    assert superuser.post("/logout", data={"csrf": "wrong"}, follow_redirects=False).status_code == 403
    assert superuser.post("/logout", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert superuser.get("/", follow_redirects=False).status_code == 302


def test_next_param_only_relative(client):
    r = client.get("/req/new", follow_redirects=False)
    assert r.headers["location"] == "/login?next=%2Freq%2Fnew"
    login(client, 1001)
    r = client.get("/login?next=https://evil.example", follow_redirects=False)
    assert r.headers["location"] == "/"
    r = client.get("/login?next=//evil.example", follow_redirects=False)
    assert r.headers["location"] == "/"


def test_admin_pages_have_security_headers(superuser):
    r = superuser.get("/")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "same-origin"


def test_session_never_expires(superuser, cfg):
    import sqlite3
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE sessions SET created_at = '2000-01-01T00:00:00', last_seen = '2000-01-01T00:00:00'")
    conn.commit(); conn.close()
    r = superuser.get("/", follow_redirects=False)
    assert r.status_code == 200
    # 访问时滑动续期，重新下发 Cookie
    assert any(c.startswith("dcpm_sid=") and "Max-Age=34560000" in c for c in r.headers.get_list("set-cookie"))


def test_single_sign_on_kicks_previous_session(superuser, client_factory):
    assert superuser.get("/", follow_redirects=False).status_code == 200
    other_device = client_factory()
    assert login(other_device, 1001, "Super").status_code == 302
    assert other_device.get("/", follow_redirects=False).status_code == 200
    # 旧设备被踢出
    assert superuser.get("/", follow_redirects=False).status_code == 302
