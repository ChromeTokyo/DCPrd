import re

from tests.conftest import create_single, csrf_of, html_file, make_zip, share_code_of


def doc_id_of(c, rid):
    return int(re.search(r"/doc/(\d+)/upload", c.get(f"/req/{rid}").text).group(1))


def test_latest_follows_and_version_fixed(superuser):
    csrf = csrf_of(superuser)
    rid = create_single(superuser, csrf, "A", file=html_file("v1.html", "内容一"))
    code = share_code_of(superuser.get(f"/req/{rid}").text)
    r = superuser.get(f"/s/{code}/")
    assert r.status_code == 200 and "内容一" in r.text
    assert r.headers["cache-control"] == "no-cache"
    assert "x-frame-options" not in r.headers
    assert r.headers["content-security-policy"].startswith("sandbox ") and "allow-same-origin" not in r.headers["content-security-policy"]
    assert superuser.get(f"/s/{code}", follow_redirects=False).status_code == 301

    superuser.post(f"/doc/{doc_id_of(superuser, rid)}/upload", data={"csrf": csrf, "note": "二"}, files={"file": html_file("v2.html", "内容二")}, follow_redirects=False)
    assert "内容二" in superuser.get(f"/s/{code}/").text
    r1 = superuser.get(f"/v/{code}/1/")
    assert "内容一" in r1.text and "immutable" in r1.headers["cache-control"]
    assert "内容二" in superuser.get(f"/v/{code}/2/").text
    assert superuser.get(f"/v/{code}/3/").status_code == 404
    assert superuser.get(f"/v/{code}/1", follow_redirects=False).status_code == 301


def test_gbk_charset_header(superuser):
    csrf = csrf_of(superuser)
    rid = create_single(superuser, csrf, "G", file=html_file("g.html", "中文", charset="gbk"))
    code = share_code_of(superuser.get(f"/req/{rid}").text)
    r = superuser.get(f"/s/{code}/")
    assert r.headers["content-type"] == "text/html; charset=gbk"
    assert r.content.decode("gbk").find("中文") > 0


def test_reset_share_old_404(superuser):
    csrf = csrf_of(superuser)
    rid = create_single(superuser, csrf, "R", file=html_file("r.html", "r"))
    old = share_code_of(superuser.get(f"/req/{rid}").text)
    assert superuser.get(f"/s/{old}/").status_code == 200
    superuser.post(f"/req/{rid}/reset-share", data={"csrf": csrf}, follow_redirects=False)
    new = share_code_of(superuser.get(f"/req/{rid}").text)
    assert new != old
    assert superuser.get(f"/s/{old}/").status_code == 404
    assert superuser.get(f"/s/{new}/").status_code == 200
    assert "reset_share" in superuser.get("/admin/audit").text


def test_path_traversal_404(superuser):
    csrf = csrf_of(superuser)
    z = make_zip({"index.html": "<img src='img/a.png'>", "img/a.png": b"png"})
    rid = create_single(superuser, csrf, "Z", file=("z.zip", z, "application/zip"))
    code = share_code_of(superuser.get(f"/req/{rid}").text)
    assert superuser.get(f"/s/{code}/img/a.png").status_code == 200
    assert superuser.get(f"/s/{code}/img/../../../original/z.zip").status_code == 404
    assert superuser.get(f"/s/{code}/..%2F..%2Foriginal%2Fz.zip").status_code == 404
    assert superuser.get(f"/s/{code}/img/").status_code == 404
    assert superuser.get(f"/s/{code}/nothing.png").status_code == 404


def test_nested_entry_redirect(superuser):
    csrf = csrf_of(superuser)
    z = make_zip({"a/proto/index.html": "deep", "a/readme.txt": "x"})
    rid = create_single(superuser, csrf, "N", file=("n.zip", z, "application/zip"))
    code = share_code_of(superuser.get(f"/req/{rid}").text)
    r = superuser.get(f"/s/{code}/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/s/{code}/proto/index.html"
    assert superuser.get(f"/s/{code}/proto/index.html").text == "deep"


def test_sandbox_toggle(superuser):
    csrf = csrf_of(superuser)
    rid = create_single(superuser, csrf, "S", file=html_file("s.html", "s"))
    code = share_code_of(superuser.get(f"/req/{rid}").text)
    r = superuser.post("/admin/settings", data={"csrf": csrf, "site_name": "X", "jira_base_url": "https://j", "timezone": "Asia/Shanghai", "max_upload_mb": "10"}, follow_redirects=False)
    assert r.status_code == 303
    assert "content-security-policy" not in superuser.get(f"/s/{code}/").headers
    assert "update_settings" in superuser.get("/admin/audit").text
    assert "<title>" in superuser.get("/").text and "X" in superuser.get("/").text


def test_compound_directory_and_convert(superuser):
    csrf = csrf_of(superuser)
    rid = create_single(superuser, csrf, "单体", file=html_file("s.html", "single"))
    detail = superuser.get(f"/req/{rid}").text
    doc_code = share_code_of(detail)
    assert superuser.post(f"/req/{rid}/convert", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    detail = superuser.get(f"/req/{rid}").text
    assert "复合" in detail and "目录页链接" in detail
    req_code = re.search(r"目录页链接.*?/s/([a-z0-9]{12})/", detail, re.S).group(1)
    assert req_code != doc_code
    # 原文档保留，历史保留
    assert superuser.get(f"/s/{doc_code}/").text.find("single") > 0
    assert superuser.get(f"/v/{doc_code}/1/").status_code == 200
    # 新增两个子文档
    superuser.post(f"/req/{rid}/docs/new", data={"csrf": csrf, "name": "子二"}, files={"file": html_file("b.html", "bbb")}, follow_redirects=False)
    superuser.post(f"/req/{rid}/docs/new", data={"csrf": csrf, "name": "子三（无文件）"}, follow_redirects=False)
    r = superuser.get(f"/s/{req_code}/")
    assert r.status_code == 200 and "单体" in r.text and "子二" in r.text and "子三" in r.text and "暂无发布版本" in r.text
    assert r.headers.get("x-frame-options") is None
    # 软删除子文档后目录页与链接不可见
    doc2_id = int(re.search(r'/doc/(\d+)">子二', superuser.get(f"/req/{rid}").text).group(1))
    doc2_code = share_code_of(superuser.get(f"/doc/{doc2_id}").text)
    assert superuser.post(f"/doc/{doc2_id}/delete", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert "子二" not in superuser.get(f"/s/{req_code}/").text
    assert superuser.get(f"/s/{doc2_code}/").status_code == 404
    assert superuser.get(f"/doc/{doc2_id}").status_code == 404
    # 删除需求后所有链接 404，列表不可见
    superuser.post(f"/req/{rid}/delete", data={"csrf": csrf}, follow_redirects=False)
    assert superuser.get(f"/s/{req_code}/").status_code == 404
    assert superuser.get(f"/s/{doc_code}/").status_code == 404
    superuser.get("/")  # 消费 flash
    assert "单体" not in superuser.get("/").text


def test_republish_and_download(superuser):
    csrf = csrf_of(superuser)
    rid = create_single(superuser, csrf, "P", file=html_file("p1.html", "one"))
    did = doc_id_of(superuser, rid)
    superuser.post(f"/doc/{did}/upload", data={"csrf": csrf}, files={"file": html_file("p2.html", "two")}, follow_redirects=False)
    ver1 = int(re.search(r"/ver/(\d+)/download", superuser.get(f"/req/{rid}").text.split("v1</strong>")[1]).group(1))
    assert superuser.post(f"/ver/{ver1}/republish", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    page = superuser.get(f"/req/{rid}").text
    assert "重新发布自 v1" in page and "v3" in page
    code = share_code_of(page)
    assert superuser.get(f"/s/{code}/").text.find("one") > 0
    r = superuser.get(f"/ver/{ver1}/download")
    assert r.status_code == 200 and "p1.html" in r.headers["content-disposition"]


def test_list_search_and_tabs(superuser):
    csrf = csrf_of(superuser)
    create_single(superuser, csrf, "支付流程", project="eb", jira_keys="DC-100")
    create_single(superuser, csrf, "聊天窗口", project="im", jira_keys="DC-200, dc-201")
    home = superuser.get("/").text
    assert "支付流程" in home and "聊天窗口" in home
    assert 'href="https://dcjira.opscom666.com/jira/browse/DC-201"' in home
    assert "聊天窗口" not in superuser.get("/?project=eb").text
    assert "支付流程" in superuser.get("/?q=DC-100").text and "聊天窗口" not in superuser.get("/?q=DC-100").text
    assert "聊天窗口" in superuser.get("/?q=聊天").text
    assert "支付流程" not in superuser.get("/?q=Super").text  # 负责人为空时搜不到
    # 设置负责人后可按负责人搜索
    uid = re.search(r'name="owners" value="(\d+)"', superuser.get("/req/1/edit").text).group(1)
    superuser.post("/req/1/edit", data={"csrf": csrf, "project": "eb", "name": "支付流程", "owners": uid}, follow_redirects=False)
    assert "支付流程" in superuser.get("/?q=Super").text
    assert "支付流程" not in superuser.get("/?q=不存在的").text


def test_healthz(client):
    r = client.get("/healthz")
    assert r.json() == {"ok": True, "version": "test"}
