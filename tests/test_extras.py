"""收藏/最近访问、导出、上传前预览、公开留言、Telegram 通知。"""
import io
import re
import zipfile

from tests.conftest import create_single, csrf_of, html_file, login, make_zip, share_code_of, tiny_png


def invite(superuser, factory, name, tg_id):
    csrf = csrf_of(superuser)
    superuser.post("/admin/users/new", data={"csrf": csrf, "name": name}, follow_redirects=False)
    code = re.findall(r"/invite/([A-Za-z0-9_\-]+)", superuser.get("/admin/users").text)[-1]
    c = factory()
    c.get(f"/invite/{code}")
    assert login(c, tg_id, name).status_code == 302
    return c


def test_favorites_and_recent(superuser):
    csrf = csrf_of(superuser)
    a = create_single(superuser, csrf, "需求甲")
    b = create_single(superuser, csrf, "需求乙")
    assert "最近访问" not in superuser.get("/").text  # 还没打开过任何需求
    superuser.get(f"/req/{a}")
    superuser.get(f"/req/{b}")
    home = superuser.get("/").text
    assert "我的收藏" not in home and "最近访问" in home and "需求乙" in home.split("最近访问")[1].split("toolbar")[0]
    # 收藏 a
    r = superuser.post(f"/req/{a}/fav", data={"csrf": csrf, "back": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    home = superuser.get("/").text
    assert "我的收藏" in home and "需求甲" in home.split("我的收藏")[1].split("最近访问")[0]
    assert "★" in superuser.get(f"/req/{a}").text.split("<h1>")[1].split("</h1>")[0]
    # 只看收藏
    fav_list = superuser.get("/?fav=1").text
    assert "需求甲" in fav_list and "需求乙" not in fav_list.split("<tbody>")[1]
    # 取消收藏
    superuser.post(f"/req/{a}/fav", data={"csrf": csrf}, follow_redirects=False)
    assert "我的收藏" not in superuser.get("/").text
    # 最近访问按时间倒序，访问 a 后排前
    superuser.get(f"/req/{a}")
    recent = superuser.get("/").text.split("最近访问")[1].split("toolbar")[0]
    assert recent.find("需求甲") < recent.find("需求乙")
    # 搜索时不显示快捷区
    assert "最近访问" not in superuser.get("/?q=甲").text
    # 非法 back 回落
    r = superuser.post(f"/req/{a}/fav", data={"csrf": csrf, "back": "https://evil"}, follow_redirects=False)
    assert r.headers["location"] == f"/req/{a}"


def test_export_zip(superuser):
    csrf = csrf_of(superuser)
    rid = create_single(superuser, csrf, "导出需求", jira_keys="DC-5", notes="备注一行", file=html_file("v1.html", "one"))
    doc_id = int(re.search(r"/doc/(\d+)/upload", superuser.get(f"/req/{rid}").text).group(1))
    superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf, "note": "第二版"}, files={"file": ("v2.zip", make_zip({"index.html": "<p>2</p>"}), "application/zip")}, follow_redirects=False)
    r = superuser.get(f"/req/{rid}/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip" and "filename" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "README.md" in names and "文档/v1-v1.html" in names and "文档/v2-v2.zip" in names
    readme = z.read("README.md").decode()
    assert "导出需求" in readme and "DC-5" in readme and "备注一行" in readme and "第二版" in readme and "| v2 |" in readme
    # 复合需求按子文档分目录
    superuser.post(f"/req/{rid}/convert", data={"csrf": csrf}, follow_redirects=False)
    superuser.post(f"/req/{rid}/docs/new", data={"csrf": csrf, "name": "子文档B"}, files={"file": html_file("b.html", "b")}, follow_redirects=False)
    z = zipfile.ZipFile(io.BytesIO(superuser.get(f"/req/{rid}/export").content))
    assert any(n.startswith("导出需求/v1-") for n in z.namelist()) and "子文档B/v1-b.html" in z.namelist()


def test_preview(superuser):
    csrf = csrf_of(superuser)
    r = superuser.post("/preview", data={"csrf": csrf}, files={"file": ("a.html", b"<h1>hi</h1><script>1</script>", "text/html")})
    assert r.status_code == 200 and "<h1>hi</h1>" in r.text and r.headers["content-security-policy"].startswith("sandbox")
    r = superuser.post("/preview", data={"csrf": csrf}, files={"file": ("a.md", b"# T\n\n|a|b|\n|-|-|\n|1|2|", "text/markdown")})
    assert "<h1" in r.text and "<table>" in r.text and "viewer.css" in r.text
    r = superuser.post("/preview", data={"csrf": csrf}, files={"file": ("a.png", tiny_png(), "image/png")})
    assert 'src="data:image/png;base64,' in r.text
    z = make_zip({"proto/index.html": "<p>", "proto/page2.html": "<p>", "proto/css/a.css": "b{}"})
    r = superuser.post("/preview", data={"csrf": csrf}, files={"file": ("p.zip", z, "application/zip")})
    assert "解压 3 个文件" in r.text and "入口文件：<code>index.html</code>" in r.text and "css/a.css" in r.text
    r = superuser.post("/preview", data={"csrf": csrf}, files={"file": ("a.pdf", b"%PDF", "application/pdf")})
    assert "只支持" in r.text
    assert superuser.post("/preview", data={"csrf": "bad"}, files={"file": ("a.html", b"x", "text/html")}).status_code == 403
    assert "预览" in superuser.get("/req/new").text and 'id="preview-modal"' in superuser.get("/req/new").text


def test_public_comments_and_notifications(app, superuser, client_factory):
    csrf = csrf_of(superuser)
    notifier = app.state.notifier
    assert notifier.dry_run
    owner = invite(superuser, client_factory, "负责人甲", 9001)
    other = invite(superuser, client_factory, "路人乙", 9002)
    owner_id = int(re.search(r'负责人甲</td>.*?/admin/users/(\d+)/', superuser.get("/admin/users").text, re.S).group(1))
    # super 创建需求，负责人甲为 owner
    rid = create_single(superuser, csrf, "留言需求", owners=owner_id, file=html_file("v1.html", "one"))
    code = share_code_of(superuser.get(f"/req/{rid}").text)
    notifier.sent.clear()
    # 路人乙上传新版本 → 通知 super（创建人）与负责人甲，不通知乙自己
    doc_id = int(re.search(r"/doc/(\d+)/upload", other.get(f"/req/{rid}").text).group(1))
    other.post(f"/doc/{doc_id}/upload", data={"csrf": csrf_of(other), "note": "乙的修改"}, files={"file": html_file("v2.html", "two")}, follow_redirects=False)
    assert sorted(t for t, _ in notifier.sent) == [1001, 9001]
    assert "路人乙 上传了「留言需求」v2" in notifier.sent[0][1] and "乙的修改" in notifier.sent[0][1] and f"/req/{rid}" in notifier.sent[0][1]
    notifier.sent.clear()
    # 待选择入口的 zip 不通知
    other.post(f"/doc/{doc_id}/upload", data={"csrf": csrf_of(other)}, files={"file": ("p.zip", make_zip({"a.html": "1", "b.html": "2"}), "application/zip")}, follow_redirects=False)
    assert notifier.sent == []
    # 公开留言（免登录）
    anon = client_factory()
    assert "commentsUrl" in anon.get(f"/s/{code}/").text
    r = anon.get(f"/s/{code}/__comments")
    assert r.status_code == 200 and r.json() == [] and r.headers["access-control-allow-origin"] == "*"
    r = anon.post(f"/s/{code}/__comments", data={"author": "", "body": "x"})
    assert r.status_code == 400
    r = anon.post(f"/s/{code}/__comments", data={"author": "测试同学", "body": "第二步按钮点不动 https://x.y", "version": "2"})
    assert r.status_code == 200 and r.json()["author"] == "测试同学" and r.json()["version"] == 2
    assert len(anon.get(f"/s/{code}/__comments").json()) == 1
    assert sorted(t for t, _ in notifier.sent) == [1001, 9001] and "测试同学 在「留言需求」留言" in notifier.sent[0][1]
    assert anon.get("/s/nonexistent00/__comments").status_code == 404
    # 后台显示、列表角标、标记处理、删除权限
    detail = superuser.get(f"/req/{rid}").text
    assert "测试同学" in detail and "第二步按钮点不动" in detail and "1 条待处理" in detail and 'href="https://x.y"' in detail
    assert "💬 1" in superuser.get("/").text
    cid = int(re.search(r"/comments/(\d+)/resolve", detail).group(1))
    assert other.post(f"/comments/{cid}/resolve", data={"csrf": csrf_of(other)}, follow_redirects=False).status_code == 303  # 任何用户可标记处理
    assert "已处理" in superuser.get(f"/req/{rid}").text and "💬" not in superuser.get("/").text
    assert other.post(f"/comments/{cid}/delete", data={"csrf": csrf_of(other)}, follow_redirects=False).status_code == 403
    assert superuser.post(f"/comments/{cid}/delete", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert anon.get(f"/s/{code}/__comments").json() == []
    assert "delete_comment" in superuser.get("/admin/audit").text
    # 限流：同一 IP 10 分钟 10 条
    for i in range(10):
        anon.post(f"/s/{code}/__comments", data={"author": "a", "body": str(i)})
    assert anon.post(f"/s/{code}/__comments", data={"author": "a", "body": "x"}).status_code == 429
    # 通知测试按钮
    notifier.sent.clear()
    r = superuser.post("/me/notify-test", data={"csrf": csrf, "back": "/"}, follow_redirects=False)
    assert r.status_code == 303 and notifier.sent and notifier.sent[0][0] == 1001 and "测试消息" in notifier.sent[0][1]
    assert "发送测试消息" in superuser.get("/").text


def test_public_export_and_original(superuser, client_factory):
    csrf = csrf_of(superuser)
    rid = create_single(superuser, csrf, "公开导出", notes="内部备注勿外泄", file=html_file("v1.html", "one"))
    doc_id = int(re.search(r"/doc/(\d+)/upload", superuser.get(f"/req/{rid}").text).group(1))
    superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf, "note": "二"}, files={"file": html_file("v2.html", "two")}, follow_redirects=False)
    code = share_code_of(superuser.get(f"/req/{rid}").text)
    anon = client_factory()
    page = anon.get(f"/s/{code}/").text
    assert f'"exportUrl": "/s/{code}/__export"' in page and f"/s/{code}/__original/1" in page and "打包下载全部版本" in page.encode("ascii", "backslashreplace").decode("unicode_escape")
    r = anon.get(f"/s/{code}/__export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "文档/v1-v1.html" in z.namelist() and "文档/v2-v2.html" in z.namelist()
    readme = z.read("README.md").decode()
    assert "公开导出" in readme and "内部备注勿外泄" not in readme and "## 留言" not in readme
    r = anon.get(f"/s/{code}/__original/1")
    assert r.status_code == 200 and "v1.html" in r.headers["content-disposition"] and b"one" in r.content
    assert anon.get(f"/s/{code}/__original/9").status_code == 404
    # 复合：目录码导整包，子文档码只导该文档
    superuser.post(f"/req/{rid}/convert", data={"csrf": csrf}, follow_redirects=False)
    superuser.post(f"/req/{rid}/docs/new", data={"csrf": csrf, "name": "子B"}, files={"file": html_file("b.html", "b")}, follow_redirects=False)
    detail = superuser.get(f"/req/{rid}").text
    dir_code = re.search(r"目录入口页.*?/s/([a-z0-9]{12})/", detail, re.S).group(1)
    dir_page = anon.get(f"/s/{dir_code}/").text
    assert f"/s/{dir_code}/__export" in dir_page
    names = zipfile.ZipFile(io.BytesIO(anon.get(f"/s/{dir_code}/__export").content)).namelist()
    assert "子B/v1-b.html" in names and any(n.startswith("公开导出/v1-") for n in names)
    b_code = re.search(r'/s/([a-z0-9]{12})/">子B', dir_page).group(1)
    names = zipfile.ZipFile(io.BytesIO(anon.get(f"/s/{b_code}/__export").content)).namelist()
    assert names == ["子B/v1-b.html", "README.md"] or set(names) == {"子B/v1-b.html", "README.md"}
    assert "打包下载本文档" in anon.get(f"/s/{b_code}/").text.encode("ascii", "backslashreplace").decode("unicode_escape")
    # 重置链接后旧码的导出也失效
    superuser.post(f"/req/{rid}/reset-share", data={"csrf": csrf}, follow_redirects=False)
    assert anon.get(f"/s/{dir_code}/__export").status_code == 404
