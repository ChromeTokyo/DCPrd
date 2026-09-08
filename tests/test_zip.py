import json
import re
from pathlib import Path

import pytest

from app.storage import UploadError, decide_entry, extract_zip
from tests.conftest import create_single, csrf_of, make_zip


def write(tmp_path, data: bytes) -> Path:
    p = tmp_path / "in.zip"
    p.write_bytes(data)
    return p


def test_zip_slip_rejected(tmp_path):
    z = write(tmp_path, make_zip({"../evil.html": "x", "ok.html": "y"}))
    with pytest.raises(UploadError):
        extract_zip(z, tmp_path / "out", 10**7)
    z = write(tmp_path, make_zip({"/abs.html": "x"}))
    with pytest.raises(UploadError):
        extract_zip(z, tmp_path / "out2", 10**7)


def test_top_level_dir_stripped_and_junk_skipped(tmp_path):
    z = write(tmp_path, make_zip({"proto/index.html": "i", "proto/images/a.png": b"p", "proto/.DS_Store": b"x", "__MACOSX/proto/._index.html": b"j", "proto/Thumbs.db": b"t"}))
    res = extract_zip(z, tmp_path / "out", 10**7)
    assert (tmp_path / "out" / "index.html").exists()
    assert (tmp_path / "out" / "images" / "a.png").exists()
    assert not (tmp_path / "out" / ".DS_Store").exists()
    assert res.file_count == 2 and res.html_paths == ["index.html"]


def test_gbk_filenames(tmp_path):
    z = write(tmp_path, make_zip({"需求/首页.html": "<p>x</p>", "需求/图片/图.png": b"p"}, names_gbk=True))
    res = extract_zip(z, tmp_path / "out", 10**7)
    assert res.html_paths == ["首页.html"]
    assert (tmp_path / "out" / "图片" / "图.png").exists()


def test_size_limit(tmp_path):
    z = write(tmp_path, make_zip({"a.html": "x" * 5000}))
    with pytest.raises(UploadError):
        extract_zip(z, tmp_path / "out", 1000)


def test_decide_entry_branches():
    assert decide_entry(["index.html", "a.html", "sub/b.html"]) == "index.html"
    assert decide_entry(["Index.HTML", "sub/b.html"]) == "Index.HTML"
    assert decide_entry(["only.html", "sub/a.html", "sub/b.html"]) == "only.html"
    assert decide_entry(["deep/inner/page.html"]) == "deep/inner/page.html"
    assert decide_entry(["a.html", "b.html"]) is None
    assert decide_entry(["x/a.html", "y/b.html"]) is None


def test_upload_zip_entry_flow(superuser):
    csrf = csrf_of(superuser)
    zip_bytes = make_zip({"p/one.html": "<b>one</b>", "p/two.html": "<b>two</b>", "p/css/s.css": "b{}"})
    loc = create_single(superuser, csrf, "zip 需求", file=("p.zip", zip_bytes, "application/zip"))
    assert isinstance(loc, str) and "/entry" in loc
    page = superuser.get(loc).text
    assert "one.html" in page and "two.html" in page and "待选择入口" not in page
    ver_id = int(re.search(r"/ver/(\d+)/entry", loc).group(1))
    # 未选入口 → 列表标记待选择、公开链接 404
    req_page = superuser.get("/req/1").text
    assert "待选择入口" in req_page
    code = re.search(r"/s/([a-z0-9]{12})/", req_page).group(1)
    assert superuser.get(f"/s/{code}/").status_code == 404
    assert superuser.post(f"/ver/{ver_id}/entry", data={"csrf": csrf, "entry": "nope.html"}, follow_redirects=False).status_code == 303
    assert superuser.post(f"/ver/{ver_id}/entry", data={"csrf": csrf, "entry": "two.html"}, follow_redirects=False).status_code == 303
    r = superuser.get(f"/s/{code}/")
    assert r.status_code == 200 and "two" in r.text
    assert superuser.get(f"/s/{code}/css/s.css").headers["content-type"].startswith("text/css")


def test_upload_rejects_non_html_zip_and_bad_zip(superuser):
    csrf = csrf_of(superuser)
    rid = create_single(superuser, csrf, "x")
    doc_id = int(re.search(r"/doc/(\d+)/upload", superuser.get(f"/req/{rid}").text).group(1))
    r = superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf}, files={"file": ("a.txt", b"x", "text/plain")}, follow_redirects=True)
    assert "只支持" in r.text
    r = superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf}, files={"file": ("a.zip", b"notazip", "application/zip")}, follow_redirects=True)
    assert "不是有效的 zip" in r.text
    r = superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf}, files={"file": ("a.zip", make_zip({"x.txt": "t"}), "application/zip")}, follow_redirects=True)
    assert "没有 html" in r.text
    assert "尚未上传任何版本" in superuser.get(f"/req/{rid}").text


def test_upload_too_large(superuser):
    csrf = csrf_of(superuser)
    rid = create_single(superuser, csrf, "big")
    doc_id = int(re.search(r"/doc/(\d+)/upload", superuser.get(f"/req/{rid}").text).group(1))
    big = b"<html>" + b"x" * (6 * 1024 * 1024)
    r = superuser.post(f"/doc/{doc_id}/upload", data={"csrf": csrf}, files={"file": ("big.html", big, "text/html")}, follow_redirects=False)
    assert r.status_code in (303, 413)
    if r.status_code == 303:
        assert "上限" in superuser.get(r.headers["location"]).text
