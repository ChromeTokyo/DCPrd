import io
import re
import time
import zipfile
import zlib
import struct

import pytest
from starlette.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.security import sign_telegram_auth

BOT_TOKEN = "123456:TEST"


def make_config(tmp_path, **overrides) -> Config:
    cfg = Config(
        domain="localhost:8000",
        tg_bot_token=BOT_TOKEN,
        tg_bot_username="dcprd_bot",
        secret_key="test-secret-key",
        max_upload_mb=5,
        data_dir=tmp_path / "data",
        dev_mode=True,
        app_version="test",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def app(cfg):
    return create_app(cfg)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


@pytest.fixture
def client_factory(app):
    clients = []

    def make():
        c = TestClient(app, base_url="http://localhost:8000")
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def tg_params(tg_id: int, first_name: str = "User", auth_date: int | None = None, token: str = BOT_TOKEN, **extra) -> dict:
    data = {"id": str(tg_id), "first_name": first_name, "auth_date": str(auth_date or int(time.time())), **extra}
    data["hash"] = sign_telegram_auth(token, data)
    return data


def login(c: TestClient, tg_id: int, first_name: str = "User"):
    return c.get("/auth/telegram", params=tg_params(tg_id, first_name), follow_redirects=False)


def csrf_of(c: TestClient, path: str = "/") -> str:
    r = c.get(path)
    m = re.search(r'name="csrf" content="([^"]+)"', r.text)
    assert m, f"no csrf on {path}: {r.status_code}"
    return m.group(1)


def share_code_of(html: str) -> str:
    m = re.search(r"/s/([a-z0-9]{12})/", html)
    assert m, "no share code in page"
    return m.group(1)


def html_file(name: str, body: str, charset: str = "utf-8") -> tuple:
    content = f"<html><head><meta charset='{charset}'></head><body>{body}</body></html>".encode(charset)
    return (name, content, "text/html")


def tiny_png() -> bytes:
    """1x1 红色 PNG。"""
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"\x00\xff\x00\x00\xff"
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class _GbkZipInfo(zipfile.ZipInfo):
    """模拟 Windows 中文压缩工具：文件名按 GBK 写入且不设 UTF-8 标记。"""

    def _encodeFilenameFlags(self):
        return self.filename.encode("gbk"), self.flag_bits


def make_zip(entries: dict[str, bytes | str], names_gbk: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            if names_gbk:
                info = _GbkZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, data)
            else:
                z.writestr(name, data)
    return buf.getvalue()


def create_single(c: TestClient, csrf: str, name: str = "需求A", project: str = "eb", file=None, **fields) -> int:
    data = {"csrf": csrf, "project": project, "kind": "single", "name": name, **fields}
    files = {"file": file} if file else None
    r = c.post("/req/new", data=data, files=files, follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    m = re.search(r"/req/(\d+)", loc)
    if m:
        return int(m.group(1))
    # 跳到了入口选择页
    return loc


@pytest.fixture
def superuser(client):
    """第一个登录者 = 超级管理员。"""
    r = login(client, 1001, "Super")
    assert r.status_code == 302
    return client
