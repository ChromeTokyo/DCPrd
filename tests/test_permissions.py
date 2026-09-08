import re

from tests.conftest import create_single, csrf_of, html_file, login


def invite_user(admin, name: str, factory, tg_id: int):
    csrf = csrf_of(admin)
    admin.post("/admin/users/new", data={"csrf": csrf, "name": name}, follow_redirects=False)
    page = admin.get("/admin/users").text
    codes = re.findall(r"/invite/([A-Za-z0-9_\-]+)", page)
    c = factory()
    c.get(f"/invite/{codes[-1]}")
    assert login(c, tg_id, name).status_code == 302
    uid = int(re.search(rf'{name}</td>.*?/admin/users/(\d+)/', admin.get("/admin/users").text, re.S).group(1))
    return c, uid


def test_permission_matrix(superuser, client_factory):
    alice, alice_id = invite_user(superuser, "Alice", client_factory, 2002)
    bob, bob_id = invite_user(superuser, "Bob", client_factory, 3003)
    a_csrf, b_csrf, s_csrf = csrf_of(alice), csrf_of(bob), csrf_of(superuser)

    rid = create_single(alice, a_csrf, "Alice 的需求", file=html_file("a.html", "hello"))
    detail = alice.get(f"/req/{rid}").text
    ver_id = int(re.search(r"/ver/(\d+)/download", detail).group(1))

    # 用户可编辑他人需求、可上传
    assert bob.post(f"/req/{rid}/edit", data={"csrf": b_csrf, "project": "im", "name": "Bob 改名"}, follow_redirects=False).status_code == 303
    assert "Bob 改名" in alice.get(f"/req/{rid}").text
    doc_id = int(re.search(r"/doc/(\d+)/upload", alice.get(f"/req/{rid}").text).group(1))
    assert bob.post(f"/doc/{doc_id}/upload", data={"csrf": b_csrf, "note": "bob v2"}, files={"file": html_file("b.html", "v2")}, follow_redirects=False).status_code == 303

    # 用户不能删他人需求；不能删任何版本
    assert bob.post(f"/req/{rid}/delete", data={"csrf": b_csrf}, follow_redirects=False).status_code == 403
    assert bob.post(f"/ver/{ver_id}/delete", data={"csrf": b_csrf}, follow_redirects=False).status_code == 403
    assert alice.post(f"/ver/{ver_id}/delete", data={"csrf": a_csrf}, follow_redirects=False).status_code == 403
    # 普通用户看不到删除按钮（自己的需求可以删）
    assert "/delete" not in bob.get(f"/req/{rid}").text.replace(f"/req/{rid}/delete", "")
    assert f"/req/{rid}/delete" in alice.get(f"/req/{rid}").text

    # 普通用户不能进后台管理
    for path in ("/admin/users", "/admin/settings", "/admin/audit"):
        assert bob.get(path).status_code == 403
    assert bob.post(f"/admin/users/{alice_id}/toggle-admin", data={"csrf": b_csrf}, follow_redirects=False).status_code == 403

    # super 设 Bob 为管理员；管理员可删版本、删他人需求；但不能 toggle-admin
    assert superuser.post(f"/admin/users/{bob_id}/toggle-admin", data={"csrf": s_csrf}, follow_redirects=False).status_code == 303
    assert "管理员" in bob.get("/").text
    users_html = bob.get("/admin/users").text
    assert "toggle-admin" not in users_html  # 管理员看不到设为管理员按钮
    assert bob.post(f"/admin/users/{alice_id}/toggle-admin", data={"csrf": b_csrf}, follow_redirects=False).status_code == 403
    assert bob.post(f"/ver/{ver_id}/delete", data={"csrf": b_csrf}, follow_redirects=False).status_code == 303
    assert bob.post(f"/req/{rid}/delete", data={"csrf": b_csrf}, follow_redirects=False).status_code == 303
    assert alice.get(f"/req/{rid}").status_code == 404

    audit = superuser.get("/admin/audit").text
    assert "delete_version" in audit and "delete_requirement" in audit and "toggle_admin" in audit

    # super 不能被删除 / 降级
    super_id = int(re.search(r"Super</td>.*?/admin/users/(\d+)/", superuser.get("/admin/users").text, re.S).group(1))
    assert bob.post(f"/admin/users/{super_id}/delete", data={"csrf": b_csrf}, follow_redirects=False).status_code == 403
    assert bob.post(f"/admin/users/{super_id}/unbind", data={"csrf": b_csrf}, follow_redirects=False).status_code == 403

    # 删除用户后其会话立即失效
    assert superuser.post(f"/admin/users/{alice_id}/delete", data={"csrf": s_csrf}, follow_redirects=False).status_code == 303
    assert alice.get("/", follow_redirects=False).status_code == 302


def test_unauthenticated_everything_redirects(client):
    for path in ("/", "/req/new", "/req/1", "/doc/1", "/ver/1/entry", "/ver/1/download", "/admin/users", "/admin/settings", "/admin/audit"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"].startswith("/login?next="), path
    for path in ("/req/new", "/req/1/delete", "/doc/1/upload", "/ver/1/delete", "/admin/users/new", "/admin/settings"):
        r = client.post(path, data={}, follow_redirects=False)
        assert r.status_code == 302, path
