"""问题单 2026-09-13：未随附本地资源检测、zip 文件名编码（Big5/cp932）、Unicode NFC。"""
import io
import json
import re
import unicodedata
import zipfile

from app.storage import detect_name_encoding, find_missing_refs, scan_local_refs
from tests.conftest import create_single, csrf_of, html_file, make_zip, share_code_of, tiny_png

SAMPLE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="css/main.css?v=3"><link rel="icon" href="favicon.ico">
<script src="js/app.js"></script><script src="https://cdn.example.com/x.js"></script>
<style>.hero{background:url("images/hero%20bg.png")} .b{background:url(data:image/png;base64,AAA)}</style>
</head><body>
<img src="規格資源/v1.1.3-D0.1/login.png"><img src="規格資源/v1.1.3-D0.1/login.png#frag">
<img srcset="參考文件/a.png 1x, 參考文件/a@2x.png 2x" src="參考文件/郵箱驗證信件範例-20260911.png">
<picture><source srcset="img/p.webp" type="image/webp"></picture>
<video src="media/demo.mp4" poster="media/poster.jpg"></video>
<div style="background-image:url('bg/side.png')"></div>
<a href="mailto:a@b.c">m</a><a href="#top">t</a><a href="tel:123">t</a>
<img src="//cdn.example.com/c.png"><img src="/static/root.png"><img src="javascript:void(0)">
</body></html>"""


def names_zip(names: dict[str, bytes], encoding: str) -> bytes:
    """按指定编码写入文件名且不设 UTF-8 标志（模拟各语言 Windows 自带压缩）。"""
    class Info(zipfile.ZipInfo):
        def _encodeFilenameFlags(self):
            return self.filename.encode(encoding), self.flag_bits

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in names.items():
            info = Info(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return buf.getvalue()


def doc_id_of(c, rid):
    return int(re.search(r"/doc/(\d+)/upload", c.get(f"/req/{rid}").text).group(1))


# ---------- 3.1 扫描 ----------

def test_scan_local_refs():
    refs = scan_local_refs(SAMPLE_HTML.encode())
    assert refs == [
        "css/main.css", "favicon.ico", "js/app.js", "images/hero bg.png",
        "規格資源/v1.1.3-D0.1/login.png", "參考文件/郵箱驗證信件範例-20260911.png", "參考文件/a.png", "參考文件/a@2x.png",
        "img/p.webp", "media/demo.mp4", "media/poster.jpg", "bg/side.png", "/static/root.png",
    ]
    assert scan_local_refs(b"<html><body><p>plain</p><img src='data:image/png;base64,x'></body></html>") == []
    # gbk 页面也能扫
    assert scan_local_refs("<html><meta charset='gbk'><img src='图片/一.png'></html>".encode("gbk")) == ["图片/一.png"]


def test_find_missing_refs_case_and_nfc(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "A.PNG").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "ok.png").write_bytes(b"x")
    (tmp_path / unicodedata.normalize("NFC", "ガ.png")).write_bytes(b"x")
    missing = find_missing_refs(tmp_path, "index.html", ["images/a.png", "sub/ok.png", "/root.png", "../up.png", "gone.png", unicodedata.normalize("NFD", "ガ.png")])
    assert missing == ["images/a.png（注意大小写：实际为 images/A.PNG）", "/root.png（根路径，托管后必然 404）", "../up.png（超出压缩包范围）", "gone.png"]
    # 入口在子目录时相对该目录解析
    assert find_missing_refs(tmp_path, "sub/page.html", ["ok.png", "../images/A.PNG"]) == []


def test_single_html_with_refs_rejected_unless_forced(superuser):
    csrf = csrf_of(superuser)
    rid = create_single(superuser, csrf, "只传 HTML")
    did = doc_id_of(superuser, rid)
    r = superuser.post(f"/doc/{did}/upload", data={"csrf": csrf}, files={"file": ("代理後台-v1.1.3.html", SAMPLE_HTML.encode(), "text/html")}, follow_redirects=True)
    msg = re.search(r'class="flash flash-err"[^>]*>([^<]+)', r.text).group(1)
    assert "引用了 13 个本地文件" in msg and "css/main.css" in msg and "規格資源/v1.1.3-D0.1/login.png" in msg and "1 个是以 / 开头的根路径" in msg and "压缩成 zip" in msg
    assert "尚未上传任何版本" in r.text
    # 勾选确认后放行，并在版本表标记缺失
    r = superuser.post(f"/doc/{did}/upload", data={"csrf": csrf, "force": "1"}, files={"file": ("代理後台-v1.1.3.html", SAMPLE_HTML.encode(), "text/html")}, follow_redirects=True)
    assert "上传成功" in r.text and "缺 13 个引用文件" in r.text and "規格資源/v1.1.3-D0.1/login.png" in r.text and "单独上传的 HTML" in r.text
    assert 'name="force"' in r.text  # 上传表单有确认框
    # 没有本地引用的 html 不受影响
    r = superuser.post(f"/doc/{did}/upload", data={"csrf": csrf}, files={"file": html_file("clean.html", "ok")}, follow_redirects=True)
    assert "上传成功，已发布为最新版本" in r.text
    # 新建需求同时上传也走同一逻辑
    r = superuser.post("/req/new", data={"csrf": csrf, "project": "eb", "kind": "single", "name": "带首文件"}, files={"file": ("x.html", b"<img src='a.png'>", "text/html")}, follow_redirects=True)
    assert "文件上传失败" in r.text and "引用了 1 个本地文件" in r.text


def test_zip_missing_refs_marked_but_accepted(superuser):
    csrf = csrf_of(superuser)
    z = make_zip({"proto/index.html": "<img src='img/a.png'><img src='img/b.png'><img src='img/c.png'><img src='Img/d.PNG'><link rel=stylesheet href='css/s.css'>",
                  "proto/img/a.png": b"1", "proto/css/s.css": "b{}", "proto/img/D.png": b"4"})
    rid = create_single(superuser, csrf, "缺图 zip", file=("p.zip", z, "application/zip"))
    detail = superuser.get(f"/req/{rid}").text
    assert "缺 3 个引用文件" in detail and "img/b.png" in detail and "img/c.png" in detail and "注意大小写" in detail and "注意大小写与目录层级" in detail
    home_flash = re.search(r'class="flash flash-warn"[^>]*>([^<]+)', superuser.get(f"/req/{rid}").text)  # flash 已被详情页消费
    code = share_code_of(detail)
    assert superuser.get(f"/s/{code}/img/a.png").status_code == 200
    assert superuser.get(f"/s/{code}/img/b.png").status_code == 404
    # 完整的包没有标记
    z2 = make_zip({"index.html": "<img src='img/a.png'>", "img/a.png": b"1"})
    superuser.post(f"/doc/{doc_id_of(superuser, rid)}/upload", data={"csrf": csrf}, files={"file": ("ok.zip", z2, "application/zip")}, follow_redirects=False)
    rows = superuser.get(f"/req/{rid}").text.split("<tbody>")[1].split("</tbody>")[0].split("<tr")
    assert "缺 3 个引用文件" in rows[1 + 1] and "缺 " not in rows[1]  # v2 在前且干净，v1 标记


def test_zip_entry_selection_computes_missing(superuser):
    csrf = csrf_of(superuser)
    z = make_zip({"a.html": "<img src='x.png'>", "b.html": "<p>b</p>", "x.png": b"1"})
    loc = create_single(superuser, csrf, "选入口", file=("p.zip", z, "application/zip"))
    vid = int(re.search(r"/ver/(\d+)/entry", loc).group(1))
    superuser.post(f"/ver/{vid}/entry", data={"csrf": csrf, "entry": "b.html"}, follow_redirects=False)
    assert "缺 " not in superuser.get("/req/1").text
    z = make_zip({"a.html": "<img src='none.png'>", "b.html": "<p>b</p>"})
    r = superuser.post(f"/doc/{doc_id_of(superuser, 1)}/upload", data={"csrf": csrf}, files={"file": ("p.zip", z, "application/zip")}, follow_redirects=False)
    vid = int(re.search(r"/ver/(\d+)/entry", r.headers["location"]).group(1))
    r = superuser.post(f"/ver/{vid}/entry", data={"csrf": csrf, "entry": "a.html"}, follow_redirects=True)
    assert "none.png" in r.text and "缺 1 个引用文件" in r.text


def test_preview_banners(superuser):
    csrf = csrf_of(superuser)
    r = superuser.post("/preview", data={"csrf": csrf}, files={"file": ("a.html", SAMPLE_HTML.encode(), "text/html")})
    body = r.content.decode("utf-8")
    import html as h
    assert "13" in body and "&#" in body  # 横条以数字实体注入，对任意编码安全
    assert "单独上传后这些图片/样式不会显示" in h.unescape(body) and body.index("<body>") < body.index("&#")
    r = superuser.post("/preview", data={"csrf": csrf}, files={"file": ("gbk.html", "<html><head><meta charset='gbk'></head><body><img src='图/a.png'></body></html>".encode("gbk"), "text/html")})
    assert "图/a.png" in h.unescape(r.content.decode("gbk"))
    z = make_zip({"index.html": "<img src='img/a.png'><img src='img/b.png'>", "img/a.png": b"1"})
    r = superuser.post("/preview", data={"csrf": csrf}, files={"file": ("p.zip", z, "application/zip")})
    assert "缺少引用文件（1）" in r.text and "img/b.png" in r.text and "img/a.png</li>" in r.text


# ---------- 3.2 编码 ----------

def test_detect_name_encoding_samples():
    def infos(names, enc):
        return [zipfile.ZipInfo(n.encode(enc).decode("cp437")) for n in names]
    assert detect_name_encoding(infos(["規格資源/v1.1.3-D0.1/login.png", "參考文件/郵箱驗證信件範例.png", "代理後台.html"], "big5")) == "big5"
    assert detect_name_encoding(infos(["需求/首页.html", "需求/图片/图.png"], "gbk")) == "gbk"
    assert detect_name_encoding(infos(["仕様書/ログイン画面.png", "index.html"], "cp932")) == "cp932"
    assert detect_name_encoding(infos(["요구사항/첫페이지.html", "이미지/그림.png"], "cp949")) == "cp949"
    assert detect_name_encoding(infos(["plain/ascii.html"], "cp437")) is None
    utf = zipfile.ZipInfo("中文.html"); utf.flag_bits |= 0x800
    assert detect_name_encoding([utf]) is None
    assert detect_name_encoding([zipfile.ZipInfo(bytes([0x80, 0x82, 0x41]).decode("cp437"))]) == "__unknown__"


def test_big5_and_cp932_zip_names_serve_correctly(superuser):
    csrf = csrf_of(superuser)
    page = "<html><head><meta charset='utf-8'></head><body><img id=a src='規格資源/v1.1.3-D0.1/login.png'><img src='參考文件/郵箱驗證信件範例-20260911.png'></body></html>"
    files = {"代理後台-v1.1.3-使用規格書-D0.1.html": page.encode(), "規格資源/v1.1.3-D0.1/login.png": tiny_png(), "參考文件/郵箱驗證信件範例-20260911.png": tiny_png()}
    rid = create_single(superuser, csrf, "Big5 包", file=("big5.zip", names_zip(files, "big5"), "application/zip"))
    detail = superuser.get(f"/req/{rid}").text
    assert "缺 " not in detail and "无法识别编码" not in detail
    code = share_code_of(detail)
    r = superuser.get(f"/s/{code}/")
    assert r.status_code == 200 and "login.png" in r.text
    assert superuser.get(f"/s/{code}/規格資源/v1.1.3-D0.1/login.png").status_code == 200
    assert superuser.get(f"/s/{code}/%E5%8F%83%E8%80%83%E6%96%87%E4%BB%B6/%E9%83%B5%E7%AE%B1%E9%A9%97%E8%AD%89%E4%BF%A1%E4%BB%B6%E7%AF%84%E4%BE%8B-20260911.png").status_code == 200
    # cp932
    page_jp = "<html><head><meta charset='utf-8'></head><body><img src='仕様書/ログイン画面.png'></body></html>"
    rid2 = create_single(superuser, csrf, "SJIS 包", file=("sjis.zip", names_zip({"index.html": page_jp.encode(), "仕様書/ログイン画面.png": tiny_png()}, "cp932"), "application/zip"))
    code2 = share_code_of(superuser.get(f"/req/{rid2}").text)
    assert superuser.get(f"/s/{code2}/仕様書/ログイン画面.png").status_code == 200
    assert "缺 " not in superuser.get(f"/req/{rid2}").text
    # gbk 老用例仍通过
    rid3 = create_single(superuser, csrf, "GBK 包", file=("gbk.zip", names_zip({"首页.html": b"<img src='\xe5\x9b\xbe\xe7\x89\x87/a.png'>", "图片/a.png": tiny_png()}, "gbk"), "application/zip"))
    code3 = share_code_of(superuser.get(f"/req/{rid3}").text)
    assert superuser.get(f"/s/{code3}/图片/a.png").status_code == 200


def test_undecodable_names_notice(superuser):
    csrf = csrf_of(superuser)
    # 文件名原始字节 img/<0x80 0x82>.png，无 UTF-8 标志：五种编码都无法解码
    data = names_zip({"index.html": b"<p>x</p>", bytes([0x69, 0x6D, 0x67, 0x2F, 0x80, 0x82, 0x2E, 0x70, 0x6E, 0x67]).decode("cp437"): b"1"}, "cp437")
    rid = create_single(superuser, csrf, "乱码包", file=("bad.zip", data, "application/zip"))
    r = superuser.get(f"/req/{rid}")
    assert "1 个无法识别编码的文件名" in r.text and "7-Zip" in r.text


# ---------- 3.3 NFC ----------

def test_nfd_zip_names_match_nfc_refs(superuser):
    csrf = csrf_of(superuser)
    nfd_dir = unicodedata.normalize("NFD", "ガイド")
    page = f"<html><head><meta charset='utf-8'></head><body><img src='{unicodedata.normalize('NFC', 'ガイド')}/ロゴ.png'></body></html>"
    z = make_zip({"index.html": page, f"{nfd_dir}/{unicodedata.normalize('NFD', 'ロゴ')}.png": tiny_png()})  # macOS 打包：NFD
    rid = create_single(superuser, csrf, "NFD 包", file=("mac.zip", z, "application/zip"))
    detail = superuser.get(f"/req/{rid}").text
    assert "缺 " not in detail
    code = share_code_of(detail)
    assert superuser.get(f"/s/{code}/ガイド/ロゴ.png").status_code == 200  # NFC 请求
    assert superuser.get(f"/s/{code}/{nfd_dir}/{unicodedata.normalize('NFD', 'ロゴ')}.png").status_code == 200  # NFD 请求也行
