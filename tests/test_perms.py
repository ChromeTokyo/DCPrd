"""项目权限：无 / 可见 / 可编辑三档，管理员全权，负责人候选过滤，迁移。"""
import re
import sqlite3

from app import db
from app.web import PROJECTS
from tests.conftest import create_single, csrf_of, grant, html_file, login, user_id_by_name


def invite_raw(superuser, factory, name, tg_id):
    """邀请并登录，但不给权限。"""
    csrf = csrf_of(superuser)
    superuser.post("/admin/users/new", data={"csrf": csrf, "name": name}, follow_redirects=False)
    code = re.findall(r"/invite/([A-Za-z0-9_\-]+)", superuser.get("/admin/users").text)[-1]
    c = factory()
    c.get(f"/invite/{code}")
    assert login(c, tg_id, name).status_code == 302
    return c, user_id_by_name(superuser, name)


def test_projects_definition():
    assert list(PROJECTS.items()) == [("eb", "EB"), ("im", "IM"), ("site", "站点"), ("tk", "TK")]


def test_none_view_edit_matrix(superuser, client_factory):
    csrf = csrf_of(superuser)
    eb = create_single(superuser, csrf, "EB 需求", project="eb", file=html_file("a.html", "eb-doc"))
    im = create_single(superuser, csrf, "IM 需求", project="im", file=html_file("b.html", "im-doc"))
    tk = create_single(superuser, csrf, "TK 需求", project="tk", file=html_file("c.html", "tk-doc"))
    eb_doc = int(re.search(r"/doc/(\d+)/upload", superuser.get(f"/req/{eb}").text).group(1))
    im_doc = int(re.search(r"/doc/(\d+)/upload", superuser.get(f"/req/{im}").text).group(1))
    im_ver = int(re.search(r"/ver/(\d+)/download", superuser.get(f"/req/{im}").text).group(1))

    user, uid = invite_raw(superuser, client_factory, "新人", 9101)
    # 新邀请：无任何权限
    home = user.get("/").text
    assert "还没有任何项目的访问权限" in home and "EB 需求" not in home and "新建需求" not in home
    assert user.get(f"/req/{eb}").status_code == 403
    assert user.get("/req/new").status_code == 403
    assert user.get("/items").status_code == 200 and "还没有任何项目的访问权限" in user.get("/items").text
    assert user.get("/items/new").status_code == 403

    # EB 可编辑、IM 可见、TK/站点 无
    grant(superuser, uid, "edit", ["eb"])
    r = superuser.post(f"/admin/users/{uid}/perms", data={"csrf": csrf, "perm_eb": "edit", "perm_im": "view"}, follow_redirects=False)
    assert r.status_code == 303
    assert "set_permissions" in superuser.get("/admin/audit").text
    ucsrf = csrf_of(user)
    home = user.get("/").text
    assert "EB 需求" in home and "IM 需求" in home and "TK 需求" not in home
    tabs = home.split('class="tabs"')[1].split("</nav>")[0]
    assert "EB" in tabs and "IM" in tabs and "TK" not in tabs and "站点" not in tabs and "👁" in tabs  # IM 只读标记
    assert "新建需求" in home
    # 搜索、项目参数越界
    assert "TK 需求" not in user.get("/?q=需求").text
    assert "TK 需求" not in user.get("/?project=tk").text

    # 无权限项目：详情 403
    assert user.get(f"/req/{tk}").status_code == 403

    # 可见项目：能看、能下载、能收藏；不能改
    d = user.get(f"/req/{im}")
    assert d.status_code == 200 and "👁 只读" in d.text and "/req/%d/edit" % im not in d.text and "上传新版本" not in d.text and "重新发布" not in d.text and "修改标签" not in d.text
    assert user.get(f"/ver/{im_ver}/download").status_code == 200
    assert user.get(f"/req/{im}/export").status_code == 200
    assert user.post(f"/req/{im}/fav", data={"csrf": ucsrf}, follow_redirects=False).status_code == 303
    for path, data in ((f"/req/{im}/edit", {"project": "im", "name": "x"}), (f"/req/{im}/tags", {}), (f"/req/{im}/convert", {}), (f"/req/{im}/reset-share", {}), (f"/req/{im}/delete", {})):
        assert user.post(path, data={"csrf": ucsrf, **data}, follow_redirects=False).status_code == 403, path
    assert user.get(f"/req/{im}/edit").status_code == 403
    r = user.post(f"/doc/{im_doc}/upload", data={"csrf": ucsrf}, files={"file": html_file("x.html", "x")}, follow_redirects=False)
    assert r.status_code == 403
    assert user.post(f"/ver/{im_ver}/republish", data={"csrf": ucsrf}, follow_redirects=False).status_code == 403

    # 可编辑项目：新建、上传、编辑都行；新建表单里只有 EB 一个项目
    form = user.get("/req/new").text
    assert 'value="eb"' in form and 'value="im"' not in form and 'value="tk"' not in form
    mine = create_single(user, ucsrf, "新人的 EB 需求", project="eb", file=html_file("m.html", "m"))
    assert user.post(f"/doc/{eb_doc}/upload", data={"csrf": ucsrf}, files={"file": html_file("y.html", "y")}, follow_redirects=False).status_code == 303
    assert user.post(f"/req/{eb}/edit", data={"csrf": ucsrf, "project": "eb", "name": "EB 需求改"}, follow_redirects=False).status_code == 303
    # 试图把需求挪到没有编辑权的项目 → 403；服务端不信表单
    assert user.post(f"/req/{mine}/edit", data={"csrf": ucsrf, "project": "tk", "name": "偷渡"}, follow_redirects=False).status_code == 403
    assert user.post("/req/new", data={"csrf": ucsrf, "project": "im", "kind": "single", "name": "只读项目新建"}, follow_redirects=False).status_code == 403
    assert user.post("/req/new", data={"csrf": ucsrf, "project": "tk", "kind": "single", "name": "无权项目新建"}, follow_redirects=False).status_code == 403
    # 收藏 / 最近访问也按可见过滤：撤掉 IM 后收藏里不再出现
    user.get(f"/req/{im}")
    assert "IM 需求" in user.get("/").text
    superuser.post(f"/admin/users/{uid}/perms", data={"csrf": csrf, "perm_eb": "edit"}, follow_redirects=False)
    home = user.get("/").text
    assert "IM 需求" not in home and user.get(f"/req/{im}").status_code == 403

    # 管理员不受矩阵影响；super 把新人设为管理员后立即全权
    assert superuser.post(f"/admin/users/{uid}/perms", data={"csrf": csrf, "perm_eb": "view"}, follow_redirects=False).status_code == 303
    superuser.post(f"/admin/users/{uid}/toggle-admin", data={"csrf": csrf}, follow_redirects=False)
    assert user.get(f"/req/{tk}").status_code == 200 and "TK 需求" in user.get("/").text
    assert "全部项目可编辑" in superuser.get("/admin/users").text
    assert "无需设置" in superuser.post(f"/admin/users/{uid}/perms", data={"csrf": csrf, "perm_eb": "none"}, follow_redirects=True).text
    # 公开链接不受权限限制
    code = re.search(r"/s/([a-z0-9]{12})/", superuser.get(f"/req/{tk}").text).group(1)
    anon = client_factory()
    assert anon.get(f"/s/{code}/").status_code == 200 and "tk-doc" in anon.get(f"/s/{code}/").text


def test_owner_candidates_filtered_by_project(superuser, client_factory):
    csrf = csrf_of(superuser)
    _, a_id = invite_raw(superuser, client_factory, "阿一", 9201)
    _, b_id = invite_raw(superuser, client_factory, "阿二", 9202)
    grant(superuser, a_id, "edit", ["eb"])
    grant(superuser, b_id, "view", ["tk"])
    form = superuser.get("/req/new").text
    # 每个候选人带 data-projects，前端按所选项目过滤
    assert re.search(r'data-projects="eb"[^>]*><input type="checkbox" name="owners" value="%d"' % a_id, form)
    assert re.search(r'data-projects="tk"[^>]*><input type="checkbox" name="owners" value="%d"' % b_id, form)
    assert 'data-projects="eb,im,site,tk"' in form  # 超管全部
    # 服务端兜底：EB 需求指定阿二为负责人 → 被丢弃，阿一保留
    rid = create_single(superuser, csrf, "负责人过滤", project="eb", owners=[a_id, b_id])
    detail = superuser.get(f"/req/{rid}").text
    owners_dd = detail.split("<dt>负责人</dt>")[1].split("</dd>")[0]
    assert "阿一" in owners_dd and "阿二" not in owners_dd
    # 重点事项同样：TK 事项的负责人只能是对 TK 有权限的人
    r = superuser.post("/items/new", data={"csrf": csrf, "project": "tk", "title": "TK 事项", "frequency": "weekly", "owners": [a_id], "reporters": [b_id]}, follow_redirects=False)
    item_id = int(re.search(r"/items/(\d+)", r.headers["location"]).group(1))
    d = superuser.get(f"/items/{item_id}").text
    owners_dd = d.split("<dt>负责人</dt>")[1].split("</dd>")[0]
    reporters_dd = d.split("<dt>汇报对象</dt>")[1].split("</dd>")[0]
    assert "阿一" not in owners_dd and "Super" in owners_dd  # 无效负责人被丢弃 → 创建人兜底
    assert "阿二" in reporters_dd
    item_form = superuser.get("/items/new").text
    assert item_form.count('class="checks people-picker"') == 2


def test_items_follow_project_permissions(superuser, client_factory):
    csrf = csrf_of(superuser)
    user, uid = invite_raw(superuser, client_factory, "看客", 9301)
    grant(superuser, uid, "view", ["eb"])
    r = superuser.post("/items/new", data={"csrf": csrf, "project": "eb", "title": "EB 事项", "frequency": "weekly", "reporters": [uid]}, follow_redirects=False)
    eb_item = int(re.search(r"/items/(\d+)", r.headers["location"]).group(1))
    r = superuser.post("/items/new", data={"csrf": csrf, "project": "tk", "title": "TK 事项", "frequency": "weekly"}, follow_redirects=False)
    tk_item = int(re.search(r"/items/(\d+)", r.headers["location"]).group(1))
    ucsrf = csrf_of(user)
    lst = user.get("/items").text
    assert "EB 事项" in lst and "TK 事项" not in lst and "新增重点事项" not in lst
    assert user.get(f"/items/{tk_item}").status_code == 403
    d = user.get(f"/items/{eb_item}")
    assert d.status_code == 200 and "👁 只读" in d.text and "/items/%d/edit" % eb_item not in d.text and 'id="nudge-open"' in d.text
    # 可见：能写进展、能催办；不能完成、不能编辑、不能新建
    assert user.post(f"/items/{eb_item}/progress", data={"csrf": ucsrf, "body": "汇报对象写进展"}, follow_redirects=False).status_code == 303
    assert user.post(f"/items/{eb_item}/nudge", data={"csrf": ucsrf}, follow_redirects=False).status_code == 303
    assert user.post(f"/items/{eb_item}/status", data={"csrf": ucsrf, "status": "done"}, follow_redirects=False).status_code == 403
    assert user.get(f"/items/{eb_item}/edit").status_code == 403
    assert user.post("/items/new", data={"csrf": ucsrf, "project": "eb", "title": "x", "frequency": "weekly"}, follow_redirects=False).status_code == 403
    # 升级为可编辑后可以新建，但表单里只有 EB
    grant(superuser, uid, "edit", ["eb"])
    form = user.get("/items/new").text
    assert 'value="eb"' in form and 'value="tk"' not in form
    assert user.post("/items/new", data={"csrf": ucsrf, "project": "tk", "title": "偷渡", "frequency": "weekly"}, follow_redirects=False).status_code == 403
    assert user.post("/items/new", data={"csrf": ucsrf, "project": "eb", "title": "合法", "frequency": "weekly"}, follow_redirects=False).status_code == 303


def test_v2_follows_visibility(superuser, client_factory):
    csrf = csrf_of(superuser)
    superuser.post("/v2/admin/systems/new", data={"csrf": csrf, "project": "tk", "name": "TK 体系"}, follow_redirects=False)
    superuser.post("/v2/admin/systems/new", data={"csrf": csrf, "project": "eb", "name": "EB 体系"}, follow_redirects=False)
    user, uid = invite_raw(superuser, client_factory, "二零", 9401)
    grant(superuser, uid, "view", ["eb"])
    menu = user.get("/v2/").text
    assert "EB 体系" in menu and "TK 体系" not in menu


def test_migration_renames_im_to_site_and_grants_existing_users(tmp_path):
    from tests.conftest import make_config
    from app.main import create_app
    cfg = make_config(tmp_path)
    create_app(cfg)  # 建出 v9 结构
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE schema_version SET version = 8")  # 假装是旧库
    now = db.utcnow()
    conn.execute("INSERT INTO users(name, role, tg_id, created_at) VALUES ('老张', 'user', 111, ?)", (now,))
    conn.execute("INSERT INTO users(name, role, tg_id, created_at) VALUES ('管理员', 'admin', 222, ?)", (now,))
    conn.execute("INSERT INTO users(name, role, invite_code, created_at) VALUES ('未绑定', 'user', 'abc', ?)", (now,))
    conn.execute("INSERT INTO requirements(project, kind, name, share_code, created_by, created_at, updated_by, updated_at) VALUES ('im', 'single', '老 IM 需求', 'aaaaaaaaaaaa', 1, ?, 1, ?)", (now, now))
    conn.execute("INSERT INTO requirements(project, kind, name, share_code, created_by, created_at, updated_by, updated_at) VALUES ('eb', 'single', 'EB', 'bbbbbbbbbbbb', 1, ?, 1, ?)", (now, now))
    conn.execute("INSERT INTO key_items(project, title, created_by, created_at, updated_by, updated_at, last_progress_at) VALUES ('im', '老 IM 事项', 1, ?, 1, ?, ?)", (now, now, now))
    conn.execute("INSERT INTO systems(project, name, created_at) VALUES ('im', '老 IM 系统', ?)", (now,))
    conn.commit(); conn.close()
    db.init_db(cfg.db_path, {})
    conn = sqlite3.connect(cfg.db_path); conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9
    assert [r["project"] for r in conn.execute("SELECT project FROM requirements ORDER BY id")] == ["site", "eb"]
    assert conn.execute("SELECT project FROM key_items").fetchone()[0] == "site"
    assert conn.execute("SELECT project FROM systems").fetchone()[0] == "site"
    perms = {(r["user_id"], r["project"]): r["level"] for r in conn.execute("SELECT * FROM project_permissions")}
    assert perms == {(1, p): "edit" for p in ("eb", "im", "site", "tk")}  # 只有已绑定的普通用户；管理员与未绑定的不写
    # 再跑一次 init_db 不会重复迁移（新建的 IM 数据不会被改成站点）
    conn.execute("INSERT INTO requirements(project, kind, name, share_code, created_by, created_at, updated_by, updated_at) VALUES ('im', 'single', '新 IM', 'cccccccccccc', 1, ?, 1, ?)", (now, now))
    conn.commit(); conn.close()
    db.init_db(cfg.db_path, {})
    conn = sqlite3.connect(cfg.db_path)
    assert conn.execute("SELECT project FROM requirements WHERE name = '新 IM'").fetchone()[0] == "im"
    conn.close()


def test_v2_write_and_content_routes_enforce_permissions(superuser, client_factory):
    """审查发现：2.0 的内容 / 下载 / 批注 / 需求单增删改 / 挂页面都要接权限。"""
    import json as _json
    from tests.test_v2 import import_snap, setup_app
    csrf = csrf_of(superuser)
    # TK 体系 + 端 + 快照（页面版本 1）
    superuser.post("/v2/admin/systems/new", data={"csrf": csrf, "project": "tk", "name": "TK 体系"}, follow_redirects=False)
    sid = re.search(r"#(\d+)</span>", superuser.get("/v2/admin").text).group(1)
    r = superuser.post("/v2/admin/apps/new", data={"csrf": csrf, "system_id": sid, "name": "TK 后台", "key": "tk-ops", "kind": "web"}, follow_redirects=False)
    app_id = int(re.search(r"/v2/admin/apps/(\d+)", r.headers["location"]).group(1))
    import_snap(superuser, csrf, app_id, "1.0", {"/merchant/list": "<h1>tk-secret</h1>"}, menu=[{"title": "列表", "route": "/merchant/list"}], app="tk-ops")
    page_id = int(re.search(r"/v2/p/(\d+)", superuser.get(f"/v2/?app={app_id}").text).group(1))
    ver_id = int(re.search(r'id="frame-new"[^>]*src="/v2/content/(\d+)', superuser.get(f"/v2/p/{page_id}").text).group(1))
    r = superuser.post("/v2/req/new", data={"csrf": csrf, "project": "tk", "name": "TK 2.0 需求", "page_id": page_id}, follow_redirects=False)
    rid = int(re.search(r"/v2/req/(\d+)", r.headers["location"]).group(1))
    # 只有 EB 可编辑的用户
    user, uid = invite_raw(superuser, client_factory, "二点零", 9501)
    grant(superuser, uid, "edit", ["eb"])
    ucsrf = csrf_of(user)
    assert user.get(f"/v2/content/{ver_id}").status_code == 403
    assert user.get(f"/v2/download/{ver_id}").status_code == 403
    assert user.get(f"/v2/annotations/{ver_id}").status_code == 403
    assert user.post(f"/v2/annotations/{ver_id}", json={"x": .1, "y": .1, "w": .1, "h": .1}, headers={"X-CSRF": ucsrf}).status_code == 403
    assert user.get(f"/v2/p/{page_id}").status_code == 403
    assert user.get(f"/v2/req/{rid}").status_code == 403 and user.get(f"/v2/req/{rid}/edit").status_code == 403
    for path, data in ((f"/v2/req/{rid}/edit", {"name": "x"}), (f"/v2/req/{rid}/status", {"status": "live"}), (f"/v2/req/{rid}/apps", {}), (f"/v2/req/{rid}/reset-share", {}), (f"/v2/req/{rid}/delete", {}),
                       (f"/v2/req/{rid}/pages/add", {"page_id": page_id}), (f"/v2/req/{rid}/pages/{page_id}/remove", {})):
        assert user.post(path, data={"csrf": ucsrf, **data}, follow_redirects=False).status_code == 403, path
    assert user.post(f"/v2/p/{page_id}/start", data={"csrf": ucsrf, "req_id": "new"}, follow_redirects=False).status_code == 403
    # 在无权项目新建 2.0 需求 → 403；GET 表单里只有 EB
    assert user.post("/v2/req/new", data={"csrf": ucsrf, "project": "tk", "name": "越权"}, follow_redirects=False).status_code == 403
    form = user.get("/v2/req/new").text
    assert 'value="eb"' in form and 'value="tk"' not in form and "people-picker" in form
    assert user.get(f"/v2/req/new?page_id={page_id}").status_code == 403
    assert "TK" not in user.get("/v2/req").text.split('class="tabs"')[1].split("</nav>")[0]
    # EB 需求不能挂 TK 页面（跨项目）
    r = user.post("/v2/req/new", data={"csrf": ucsrf, "project": "eb", "name": "EB 2.0"}, follow_redirects=False)
    eb_rid = int(re.search(r"/v2/req/(\d+)", r.headers["location"]).group(1))
    assert user.post(f"/v2/req/{eb_rid}/pages/add", data={"csrf": ucsrf, "page_id": page_id}, follow_redirects=False).status_code == 403
    assert superuser.post(f"/v2/req/{eb_rid}/pages/add", data={"csrf": csrf, "page_id": page_id}, follow_redirects=False).status_code == 400  # 管理员也不能跨项目挂
    # 可见（非可编辑）用户：能看内容，不能批注、不能改需求单
    grant(superuser, uid, "view", ["tk"])
    assert user.get(f"/v2/content/{ver_id}").status_code == 200 and "tk-secret" in user.get(f"/v2/content/{ver_id}").text
    assert user.get(f"/v2/annotations/{ver_id}").status_code == 200
    assert user.post(f"/v2/annotations/{ver_id}", json={"x": .1, "y": .1, "w": .1, "h": .1}, headers={"X-CSRF": ucsrf}).status_code == 403
    assert user.get(f"/v2/req/{rid}").status_code == 200 and user.get(f"/v2/req/{rid}/edit").status_code == 403
    assert "基于此页发起改动" not in user.get(f"/v2/p/{page_id}").text
    # 2.0 需求负责人服务端过滤：无 TK 权限的人被丢弃
    _, other = invite_raw(superuser, client_factory, "局外人", 9502)
    superuser.post(f"/v2/req/{rid}/edit", data={"csrf": csrf, "name": "TK 2.0 需求", "owners": [other, uid]}, follow_redirects=False)
    d = superuser.get(f"/v2/req/{rid}").text
    owners_dd = d.split("<dt>负责人</dt>")[1].split("</dd>")[0]
    assert "二点零" in owners_dd and "局外人" not in owners_dd


def test_revoke_cleans_assignments_and_notifications(app, superuser, client_factory):
    csrf = csrf_of(superuser)
    notifier = app.state.notifier
    user, uid = invite_raw(superuser, client_factory, "小赵", 9601)
    grant(superuser, uid, "edit", ["eb"])
    rid = create_single(superuser, csrf, "EB 需求", project="eb", owners=[uid], file=html_file("a.html", "1"))
    r = superuser.post("/items/new", data={"csrf": csrf, "project": "eb", "title": "EB 事项", "frequency": "weekly", "owners": [uid]}, follow_redirects=False)
    item_id = int(re.search(r"/items/(\d+)", r.headers["location"]).group(1))
    assert "小赵" in superuser.get(f"/req/{rid}").text.split("<dt>负责人</dt>")[1].split("</dd>")[0]
    # 撤掉 EB → 负责人记录移除，且后续通知不再发给他
    superuser.post(f"/admin/users/{uid}/perms", data={"csrf": csrf}, follow_redirects=False)
    assert "小赵" not in superuser.get(f"/req/{rid}").text.split("<dt>负责人</dt>")[1].split("</dd>")[0]
    assert "小赵" not in superuser.get(f"/items/{item_id}").text.split("<dt>负责人</dt>")[1].split("</dd>")[0]
    assert "removed_assignments" in superuser.get("/admin/audit").text
    # 即便留了收藏记录，也不再收到该项目通知
    grant(superuser, uid, "view", ["eb"])
    user.post(f"/req/{rid}/fav", data={"csrf": csrf_of(user)}, follow_redirects=False)
    superuser.post(f"/admin/users/{uid}/perms", data={"csrf": csrf}, follow_redirects=False)
    notifier.sent.clear()
    doc_id = int(re.search(r"/doc/(\d+)/upload", superuser.get(f"/req/{rid}").text).group(1))
    superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf}, files={"file": html_file("b.html", "2")}, follow_redirects=False)
    assert notifier.sent == [] and "上传了「EB 需求」v2" not in user.get("/notifications?all=1").text
    # 解绑后权限清空：新绑定的人从零开始
    superuser.post(f"/admin/users/{uid}/perms", data={"csrf": csrf, "perm_eb": "edit"}, follow_redirects=False)
    superuser.post(f"/admin/users/{uid}/unbind", data={"csrf": csrf}, follow_redirects=False)
    code = re.search(r'小赵</span>.*?/invite/([A-Za-z0-9_\-]+)', superuser.get("/admin/users").text, re.S).group(1)
    newbie = client_factory()
    newbie.get(f"/invite/{code}")
    assert login(newbie, 9602, "New").status_code == 302
    assert "还没有任何项目的访问权限" in newbie.get("/").text


def test_item_linked_requirements_filtered(superuser, client_factory):
    csrf = csrf_of(superuser)
    tk = create_single(superuser, csrf, "TK 机密需求", project="tk")
    eb = create_single(superuser, csrf, "EB 需求", project="eb")
    user, uid = invite_raw(superuser, client_factory, "小钱", 9701)
    grant(superuser, uid, "edit", ["eb"])
    ucsrf = csrf_of(user)
    assert "TK 机密需求" not in user.get("/items/new").text
    r = user.post("/items/new", data={"csrf": ucsrf, "project": "eb", "title": "关联测试", "frequency": "weekly", "requirements": [tk, eb]}, follow_redirects=False)
    item_id = int(re.search(r"/items/(\d+)", r.headers["location"]).group(1))
    d = user.get(f"/items/{item_id}").text
    assert "EB 需求" in d and "TK 机密需求" not in d
    # 管理员挂了 TK 需求，普通用户看事项时也看不到它
    superuser.post(f"/items/{item_id}/edit", data={"csrf": csrf, "project": "eb", "title": "关联测试", "frequency": "weekly", "owners": [uid], "requirements": [tk, eb]}, follow_redirects=False)
    assert "TK 机密需求" in superuser.get(f"/items/{item_id}").text and "TK 机密需求" not in user.get(f"/items/{item_id}").text
    # 负责人候选的 data-projects 只暴露当前用户可见的项目
    assert 'data-projects="eb"' in user.get("/req/new").text and 'data-projects="eb,im,site,tk"' not in user.get("/req/new").text


def test_public_widget_has_site_color():
    from app.public_widget import build_widget
    assert b".tag-site{background:#10b981}" in build_widget({"req": {"name": "x", "project": "site", "projectLabel": "站点", "owners": [], "jira": [], "notes": ""}, "doc": {"name": "d", "compound": False, "dirUrl": None, "notes": ""}, "current": 1, "latest": 1, "latestUrl": "/s/a/", "versions": [], "notesKey": ""})
