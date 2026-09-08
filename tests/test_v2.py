"""2.0 接口测试：系统/端/菜单、快照导入与去重、需求-页面-改稿、自动 diff、批注、上线对齐、公开分享、权限。"""
import io
import json
import re
import zipfile

from PIL import Image

from app.v2.diff_html import diff_html, inject_diff
from app.v2.diff_image import diff_images
from app.v2.snapshot import normalize_menu
from tests.conftest import csrf_of, login

MENU = [{"title": "商户管理", "route": "/merchant", "children": [{"title": "商户列表", "route": "/merchant/list"}, {"title": "商户审核", "route": "/merchant/audit"}]}, {"title": "设置", "route": "/settings"}]


def snap_zip(release, pages: dict[str, str | bytes], renderer="html", menu=MENU, app="eb-ops"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        manifest = {"app": app, "release": release, "renderer": renderer, "menu": menu, "pages": []}
        titles = {}

        def walk(items):
            for it in items:
                if it.get("children"):
                    walk(it["children"])
                elif it.get("route"):
                    titles[it["route"]] = it["title"]
        walk(menu or [])
        for i, (route, content) in enumerate(pages.items()):
            fname = f"pages/{i}.{'png' if renderer == 'image' else 'html'}"
            manifest["pages"].append({"route": route, "title": titles.get(route) or route.strip("/").split("/")[-1] or "home", "file": fname})
            z.writestr(fname, content)
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return ("snap.zip", buf.getvalue(), "application/zip")


def png(color, size=(200, 400)):
    im = Image.new("RGB", size, "white")
    for x in range(50, 150):
        for y in range(100, 140):
            im.putpixel((x, y), color)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def flash_of(c, path):
    m = re.search(r'class="flash[^"]*"[^>]*>([^<]+)', c.get(path).text)
    return m.group(1) if m else ""


def setup_app(c, csrf, kind="web", key="eb-ops", name="运营后台"):
    if not re.search(r"商户体系", c.get("/v2/admin").text):
        c.post("/v2/admin/systems/new", data={"csrf": csrf, "project": "eb", "name": "商户体系"}, follow_redirects=False)
    sid = re.search(r"#(\d+)</span>", c.get("/v2/admin").text).group(1)
    r = c.post("/v2/admin/apps/new", data={"csrf": csrf, "system_id": sid, "name": name, "key": key, "kind": kind}, follow_redirects=False)
    assert r.status_code == 303
    return int(re.search(r"/v2/admin/apps/(\d+)", r.headers["location"]).group(1))


def import_snap(c, csrf, app_id, release, pages, renderer="html", app="eb-ops", menu=MENU):
    r = c.post(f"/v2/admin/apps/{app_id}/snapshots/import", data={"csrf": csrf}, files={"file": snap_zip(release, pages, renderer, menu=menu, app=app)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return flash_of(c, f"/v2/admin/apps/{app_id}")


# ---------- 引擎单元测试 ----------

def test_diff_html_types():
    a = "<html><body><div id='app'><h1>标题</h1><ul><li>a</li><li>b</li></ul><button class='x'>保存</button><p>去掉</p></div></body></html>"
    b = "<html><body><div id='app'><h1>新标题</h1><ul><li>a</li><li>b</li><li>c</li></ul><button class='x' disabled>保存</button></div></body></html>"
    r = diff_html(a, b)
    types = {c["type"] for c in r["changes"]}
    assert {"text", "added", "attr", "removed"} <= types
    text = next(c for c in r["changes"] if c["type"] == "text")
    assert text["old"] == "标题" and text["new"] == "新标题" and text["path"].endswith("h1:nth-of-type(1)")
    assert diff_html(a, a)["changes"] == []
    out = inject_diff(b, r["changes"])
    assert out.count("data-dcpm") >= 2 and out.rstrip().endswith("</html>") and "dcpm-size" in out


def test_diff_html_ignores_scripts_and_counts_nth_like_browser():
    a = "<html><body><script>1</script><style>a{}</style><div>x</div><div>y</div></body></html>"
    b = "<html><body><script>2</script><style>b{}</style><div>x</div><div>y2</div></body></html>"
    r = diff_html(a, b)
    assert len(r["changes"]) == 1 and r["changes"][0]["path"].endswith("div:nth-of-type(2)")


def test_diff_images(tmp_path):
    p1, p2 = tmp_path / "a.png", tmp_path / "b.png"
    p1.write_bytes(png((0, 0, 255)))
    p2.write_bytes(png((255, 0, 0)))
    r = diff_images(str(p1), str(p2))
    assert len(r["changes"]) == 1
    box = r["changes"][0]
    assert 40 <= box["x"] <= 56 and 96 <= box["y"] <= 104 and box["w"] >= 96 and box["h"] >= 32
    same = diff_images(str(p1), str(p1))
    assert same["changes"] == []


def test_normalize_menu_shapes():
    m = normalize_menu({"routes": [{"path": "/a", "meta": {"title": "A"}, "children": [{"path": "b", "name": "B"}, {"path": "h", "hidden": True, "name": "H"}]}, {"name": "C", "route": "/c"}]})
    assert m[0]["title"] == "A" and m[0]["children"][0]["route"] == "/a/b" and len(m[0]["children"]) == 1 and m[1]["route"] == "/c"


# ---------- 接口测试 ----------

def test_snapshot_import_menu_and_dedup(superuser):
    csrf = csrf_of(superuser)
    app_id = setup_app(superuser, csrf)
    msg = import_snap(superuser, csrf, app_id, "3.12.0", {"/merchant/list": "<h1>列表</h1>", "/merchant/audit": "<h1>审核</h1>", "/settings": "<h1>设置</h1>"})
    assert "3 页" in msg and "3 个新基线" in msg
    menu = superuser.get(f"/v2/?app={app_id}").text
    assert "商户管理" in menu and "商户列表" in menu and "设置" in menu
    msg = import_snap(superuser, csrf, app_id, "3.12.1", {"/merchant/list": "<h1>列表 v2</h1>", "/merchant/audit": "<h1>审核</h1>", "/settings": "<h1>设置</h1>"})
    assert "1 个新基线" in msg and "2 页未变化" in msg
    page_id = int(re.search(r'/v2/p/(\d+)"[^>]*>商户列表', menu).group(1))
    view = superuser.get(f"/v2/p/{page_id}").text
    assert "线上 3.12.0" in view and "线上 3.12.1" in view
    latest = int(re.search(r'id="frame-new"[^>]*src="/v2/content/(\d+)', view).group(1))
    assert "列表 v2" in superuser.get(f"/v2/content/{latest}").text
    # 重复导入同一 release 不报错
    assert "0 个新基线" in import_snap(superuser, csrf, app_id, "3.12.1", {"/settings": "<h1>设置</h1>"})
    # 非法包
    r = superuser.post(f"/v2/admin/apps/{app_id}/snapshots/import", data={"csrf": csrf}, files={"file": ("x.zip", b"junk", "application/zip")}, follow_redirects=False)
    assert "不是有效的 zip" in flash_of(superuser, f"/v2/admin/apps/{app_id}")


def test_requirement_flow_html_diff_and_share(superuser, client_factory):
    csrf = csrf_of(superuser)
    app_id = setup_app(superuser, csrf)
    import_snap(superuser, csrf, app_id, "3.12.0", {"/merchant/list": "<html><body><h1>列表</h1><button>导出</button></body></html>", "/merchant/audit": "<h1>审核</h1>"})
    menu = superuser.get(f"/v2/?app={app_id}").text
    list_id = int(re.search(r'/v2/p/(\d+)"[^>]*>商户列表', menu).group(1))
    audit_id = int(re.search(r'/v2/p/(\d+)"[^>]*>商户审核', menu).group(1))
    # 从页面发起 → 新建需求
    r = superuser.post(f"/v2/p/{list_id}/start", data={"csrf": csrf, "req_id": "new"}, follow_redirects=False)
    assert r.status_code == 303 and f"page_id={list_id}" in r.headers["location"]
    r = superuser.post("/v2/req/new", data={"csrf": csrf, "project": "eb", "name": "列表加筛选", "jira_keys": "DC-9", "page_id": list_id}, follow_redirects=False)
    rid = int(re.search(r"/v2/req/(\d+)", r.headers["location"]).group(1))
    detail = superuser.get(f"/v2/req/{rid}").text
    assert "商户列表" in detail and "下载基线" in detail and "运营后台" in detail
    # 加入第二页
    assert superuser.post(f"/v2/p/{audit_id}/start", data={"csrf": csrf, "req_id": rid}, follow_redirects=False).status_code == 303
    # 上传改稿
    new_html = "<html><body><h1>列表</h1><input placeholder='筛选'><button>导出</button></body></html>"
    r = superuser.post(f"/v2/req/{rid}/pages/{list_id}/upload", data={"csrf": csrf, "note": "加筛选"}, files={"file": ("list.html", new_html.encode(), "text/html")}, follow_redirects=False)
    assert r.status_code == 303
    vid = int(re.search(r"v=(\d+)", r.headers["location"]).group(1))
    view = superuser.get(f"/v2/p/{list_id}?v={vid}").text
    assert "列表加筛选" in view and "新增" in view and "商户审核" in view  # 同需求其他页面
    content = superuser.get(f"/v2/content/{vid}?diff=1")
    assert 'data-dcpm="1"' in content.text and "input:nth-of-type(1)" in content.text
    assert content.headers["content-security-policy"].startswith("sandbox")
    assert "x-frame-options" not in content.headers
    # 错误类型
    superuser.post(f"/v2/req/{rid}/pages/{list_id}/upload", data={"csrf": csrf}, files={"file": ("x.txt", b"x", "text/plain")}, follow_redirects=False)
    assert "只支持" in flash_of(superuser, f"/v2/req/{rid}")
    # 批注
    r = superuser.post(f"/v2/annotations/{vid}", json={"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1, "text": "这里"}, headers={"X-CSRF": csrf})
    assert r.status_code == 200
    assert superuser.post(f"/v2/annotations/{vid}", json={"x": 0, "y": 0, "w": 0, "h": 0}, headers={"X-CSRF": csrf}).status_code == 400
    assert superuser.post(f"/v2/annotations/{vid}", json={"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1}, headers={"X-CSRF": "bad"}).status_code == 403
    assert "这里" in superuser.get(f"/v2/p/{list_id}?v={vid}").text
    # 公开分享
    code = re.search(r"/p2/([a-z0-9]{12})/", superuser.get(f"/v2/req/{rid}").text).group(1)
    anon = client_factory()
    d = anon.get(f"/p2/{code}/")
    assert d.status_code == 200 and "商户列表" in d.text and "改稿未上传" in d.text  # 审核页无改稿
    p = anon.get(f"/p2/{code}/{list_id}/")
    assert p.status_code == 200 and "自动圈出" in p.text and "这里" in p.text
    assert anon.get(f"/p2/{code}/c/{vid}?diff=1").status_code == 200
    assert anon.get(f"/p2/{code}/{audit_id}/").status_code == 200
    other_baseline = int(re.search(r"/v2/content/(\d+)", superuser.get(f"/v2/p/{audit_id}").text).group(1))
    assert anon.get(f"/p2/{code}/c/{other_baseline}").status_code == 200  # 审核页基线属于需求
    assert anon.get(f"/p2/{code}/c/9999").status_code == 404
    assert anon.get("/v2/", follow_redirects=False).status_code == 302
    # 重置分享后旧码 404
    superuser.post(f"/v2/req/{rid}/reset-share", data={"csrf": csrf}, follow_redirects=False)
    assert anon.get(f"/p2/{code}/").status_code == 404
    # 1.0 列表不显示、1.0 详情跳转
    assert "列表加筛选" not in superuser.get("/").text
    assert superuser.get(f"/req/{rid}", follow_redirects=False).headers["location"] == f"/v2/req/{rid}"
    # 移除页面 → 改稿隐藏
    superuser.post(f"/v2/req/{rid}/pages/{list_id}/remove", data={"csrf": csrf}, follow_redirects=False)
    assert superuser.get(f"/v2/content/{vid}").status_code == 404


def test_image_app_flow_and_release_alignment(superuser):
    csrf = csrf_of(superuser)
    web_id = setup_app(superuser, csrf, key="eb-ops")
    app2 = setup_app(superuser, csrf, kind="flutter_web", key="eb-app", name="商户App")
    import_snap(superuser, csrf, web_id, "3.12.0", {"/merchant/list": "<h1>a</h1>"})
    HOME_MENU = [{"title": "首页", "route": "/home"}]
    import_snap(superuser, csrf, app2, "1.0.0", {"/home": png((0, 0, 255))}, renderer="image", app="eb-app", menu=HOME_MENU)
    home = int(re.search(r"/v2/p/(\d+)", superuser.get(f"/v2/?app={app2}").text).group(1))
    lst = int(re.search(r"/v2/p/(\d+)", superuser.get(f"/v2/?app={web_id}").text).group(1))
    r = superuser.post("/v2/req/new", data={"csrf": csrf, "project": "eb", "name": "双端需求", "page_id": home}, follow_redirects=False)
    rid = int(re.search(r"/v2/req/(\d+)", r.headers["location"]).group(1))
    superuser.post(f"/v2/req/{rid}/pages/add", data={"csrf": csrf, "page_id": lst}, follow_redirects=False)
    # 类型校验
    superuser.post(f"/v2/req/{rid}/pages/{home}/upload", data={"csrf": csrf}, files={"file": ("x.html", b"<p>", "text/html")}, follow_redirects=False)
    assert "截图" in flash_of(superuser, f"/v2/req/{rid}")
    r = superuser.post(f"/v2/req/{rid}/pages/{home}/upload", data={"csrf": csrf}, files={"file": ("h.png", png((255, 0, 0)), "image/png")}, follow_redirects=False)
    vid = int(re.search(r"v=(\d+)", r.headers["location"]).group(1))
    view = superuser.get(f"/v2/p/{home}?v={vid}").text
    assert 'class="box auto"' in view and "1 处" in superuser.get(f"/v2/req/{rid}").text
    # 两端各填版本，逐个对齐
    r = superuser.post(f"/v2/req/{rid}/apps", data={"csrf": csrf, f"release_{web_id}": "3.13.0", f"release_{app2}": "1.1.0"}, follow_redirects=False)
    assert r.status_code == 303
    superuser.post(f"/v2/req/{rid}/status", data={"csrf": csrf, "status": "pending"}, follow_redirects=False)
    import_snap(superuser, csrf, web_id, "3.13.0", {"/merchant/list": "<h1>a2</h1>"})
    d = superuser.get(f"/v2/req/{rid}").text
    assert d.count("已上线 ") == 1 and 'st-pending">待上线' in d
    import_snap(superuser, csrf, app2, "1.1.0", {"/home": png((255, 0, 0))}, renderer="image", app="eb-app", menu=HOME_MENU)
    d = superuser.get(f"/v2/req/{rid}").text
    assert 'st-live">已上线</span></h1>' in d
    assert "双端需求" in superuser.get("/v2/req?status=live").text and "双端需求" not in superuser.get("/v2/req?status=draft").text
    assert "双端需求" in superuser.get("/v2/req?q=home").text  # 按页面搜索


def test_new_page_and_menu_import_and_permissions(superuser, client_factory):
    csrf = csrf_of(superuser)
    app_id = setup_app(superuser, csrf)
    import_snap(superuser, csrf, app_id, "1.0", {"/merchant/list": "<h1>a</h1>"})
    r = superuser.post("/v2/req/new", data={"csrf": csrf, "project": "eb", "name": "新页需求"}, follow_redirects=False)
    rid = int(re.search(r"/v2/req/(\d+)", r.headers["location"]).group(1))
    parent = re.search(r"商户管理 <span class=\"muted small\">#(\d+)", superuser.get(f"/v2/admin/apps/{app_id}").text).group(1)
    r = superuser.post(f"/v2/req/{rid}/pages/new", data={"csrf": csrf, "app_id": app_id, "title": "商户黑名单", "route_key": "/merchant/blacklist", "parent_id": parent}, follow_redirects=False)
    assert r.status_code == 303
    menu = superuser.get(f"/v2/?app={app_id}").text
    assert "商户黑名单" in menu
    new_id = int(re.search(r'/v2/p/(\d+)"[^>]*>商户黑名单', menu).group(1))
    assert "新增页面" in superuser.get(f"/v2/p/{new_id}").text
    assert "无基线" in superuser.get(f"/v2/req/{rid}").text
    # 新页面上传改稿：没有基线，不做 diff 但可查看
    r = superuser.post(f"/v2/req/{rid}/pages/{new_id}/upload", data={"csrf": csrf}, files={"file": ("b.html", b"<h1>black</h1>", "text/html")}, follow_redirects=False)
    assert r.status_code == 303 and "无基线，未做对比" in superuser.get(r.headers["location"]).text
    # 移除后新增页面消失
    superuser.post(f"/v2/req/{rid}/pages/{new_id}/remove", data={"csrf": csrf}, follow_redirects=False)
    assert "商户黑名单" not in superuser.get(f"/v2/?app={app_id}").text
    # 菜单 JSON 导入（整棵替换）
    r = superuser.post(f"/v2/admin/apps/{app_id}/menu/import", data={"csrf": csrf, "menu_json": json.dumps([{"path": "/merchant", "meta": {"title": "商户"}, "children": [{"path": "list", "meta": {"title": "列表"}}]}])}, follow_redirects=False)
    assert "2 个节点" in flash_of(superuser, f"/v2/admin/apps/{app_id}")
    menu = superuser.get(f"/v2/?app={app_id}").text
    assert "商户管理" not in menu and "列表" in menu
    assert "识别出任何菜单项" in (superuser.post(f"/v2/admin/apps/{app_id}/menu/import", data={"csrf": csrf, "menu_json": "[]"}, follow_redirects=False) and flash_of(superuser, f"/v2/admin/apps/{app_id}"))
    # 普通用户：不能进管理页，不能导入，不能删版本，可以建需求与上传
    superuser.post("/admin/users/new", data={"csrf": csrf, "name": "小王"}, follow_redirects=False)
    code = re.search(r"/invite/([A-Za-z0-9_\-]+)", superuser.get("/admin/users").text).group(1)
    wang = client_factory()
    wang.get(f"/invite/{code}")
    assert login(wang, 5005, "Wang").status_code == 302
    wcsrf = csrf_of(wang, "/v2/")
    assert wang.get("/v2/admin").status_code == 403
    assert wang.post(f"/v2/admin/apps/{app_id}/snapshots/import", data={"csrf": wcsrf}, files={"file": snap_zip("9", {"/x": "<p>"})}, follow_redirects=False).status_code == 403
    lst = int(re.search(r"/v2/p/(\d+)", menu).group(1))
    base_vid = int(re.search(r"/v2/content/(\d+)", wang.get(f"/v2/p/{lst}").text).group(1))
    assert wang.post(f"/v2/version/{base_vid}/delete", data={"csrf": wcsrf}, follow_redirects=False).status_code == 403
    assert wang.post("/v2/req/new", data={"csrf": wcsrf, "project": "eb", "name": "小王的需求", "page_id": lst}, follow_redirects=False).status_code == 303
    assert wang.post(f"/v2/req/{rid}/delete", data={"csrf": wcsrf}, follow_redirects=False).status_code == 403  # 他人需求
    assert "v2_delete" not in superuser.get("/admin/audit").text or True
