"""端到端测试（Playwright + Chromium）：启动真实 uvicorn，走完整业务流。"""
from __future__ import annotations

import re
import socket
import threading
import time
from urllib.parse import urlencode

import pytest
import uvicorn

from app.main import create_app
from tests.conftest import make_config, make_zip, tg_params, tiny_png

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_url(tmp_path_factory):
    port = _free_port()
    cfg = make_config(tmp_path_factory.mktemp("e2e"), domain=f"127.0.0.1:{port}")
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def tg_login(page, base_url: str, tg_id: int, name: str):
    page.goto(f"{base_url}/auth/telegram?{urlencode(tg_params(tg_id, name))}")


def share_url(page) -> str:
    return page.locator(".share-row input").first.input_value()


def confirm(page):
    page.locator("#confirm-ok").click()


def test_full_flow(browser, live_url):
    base_url = live_url
    admin_ctx = browser.new_context()
    admin = admin_ctx.new_page()

    # 1. 初始化：第一个登录者成为超管
    admin.goto(f"{base_url}/login")
    assert "系统初始化" in admin.content()
    tg_login(admin, base_url, 1001, "Super")
    admin.wait_for_url(f"{base_url}/")
    assert "超管" in admin.content()

    # 2. 新建用户与邀请
    admin.goto(f"{base_url}/admin/users")
    admin.fill("input[name=name]", "张三")
    admin.click("text=创建并生成邀请链接")
    invite_url = admin.locator(".invite").first.inner_text().strip()
    assert "/invite/" in invite_url

    # 3. 第二个身份通过邀请绑定
    user_ctx = browser.new_context()
    user = user_ctx.new_page()
    user.goto(invite_url)
    assert "你好" in user.content() and "张三" in user.content()
    tg_login(user, base_url, 2002, "Zhang")
    user.wait_for_url(f"{base_url}/")
    assert "张三" in user.content()
    assert "用户管理" not in user.locator(".nav").inner_text()

    # super 能看到"设为管理员"，普通用户看不到用户管理页
    admin.goto(f"{base_url}/admin/users")
    assert admin.locator("text=设为管理员").count() == 1

    # 4. 单体需求 + 上传 html v1
    admin.goto(f"{base_url}/req/new")
    admin.check("input[name=project][value=eb]")
    admin.fill("input[name=name]", "支付流程改版")
    admin.fill("input[name=jira_keys]", "DC-85989")
    admin.set_input_files("input[name=file]", {"name": "v1.html", "mimeType": "text/html", "buffer": "<html><body><h1 id='t'>版本一</h1></body></html>".encode()})
    admin.click("button:has-text('创建')")
    admin.wait_for_url(re.compile(r"/req/\d+$"))
    req1_url = admin.url
    assert "v1" in admin.content()
    assert admin.locator("a.jira[href='https://dcjira.opscom666.com/jira/browse/DC-85989']").count() == 1
    public1 = share_url(admin)

    # 5. 公开链接免登录可打开
    anon_ctx = browser.new_context()
    anon = anon_ctx.new_page()
    resp = anon.goto(public1)
    assert resp.status == 200 and anon.locator("#t").inner_text() == "版本一"

    # 6. 上传 v2：/s/ 跟随最新，/v/../1/ 仍是旧内容
    admin.goto(req1_url)
    admin.set_input_files("section.card input[name=file]", {"name": "v2.html", "mimeType": "text/html", "buffer": "<html><body><h1 id='t'>版本二</h1></body></html>".encode()})
    admin.fill("section.card input[name=note]", "第二版")
    admin.click("section.card button:has-text('上传')")
    admin.wait_for_url(req1_url)
    assert "第二版" in admin.content() and "v2" in admin.content()
    anon.goto(public1)
    assert anon.locator("#t").inner_text() == "版本二"
    # 右下角需求信息小菜单：点开后显示需求名、负责人、两个历史版本
    anon.click("#dcpm-widget-host .pill")
    panel = anon.locator("#dcpm-widget-host .panel")
    assert panel.is_visible()
    txt = panel.inner_text()
    assert "支付流程改版" in txt and "负责人" in txt and "v2" in txt and "v1" in txt and "DC-85989" in txt
    assert anon.locator("#dcpm-widget-host .vs li").count() == 2
    anon.click("#dcpm-widget-host .vs li:has-text('v1') a")
    anon.wait_for_url(re.compile(r"/v/[a-z0-9]{12}/1/"))
    assert anon.locator("#t").inner_text() == "版本一"
    assert "历史版本" in anon.locator("#dcpm-widget-host .pill").inner_text()
    code = re.search(r"/s/([a-z0-9]{12})/", public1).group(1)
    anon.goto(f"{base_url}/v/{code}/1/")
    assert anon.locator("#t").inner_text() == "版本一"

    # 7. 复合需求 + 两个子文档 + zip（含 images/）
    admin.goto(f"{base_url}/req/new")
    admin.check("input[name=project][value=im]")
    admin.check("#kind-compound")
    admin.fill("input[name=name]", "IM 复合需求")
    admin.click("button:has-text('创建')")
    admin.wait_for_url(re.compile(r"/req/\d+$"))
    req2_url = admin.url
    zip_bytes = make_zip({"proto/index.html": "<html><body><img id='pic' src='images/dot.png'><p>原型</p></body></html>", "proto/images/dot.png": tiny_png()})
    admin.fill("form[action$='/docs/new'] input[name=name]", "文档A")
    admin.set_input_files("form[action$='/docs/new'] input[name=file]", {"name": "proto.zip", "mimeType": "application/zip", "buffer": zip_bytes})
    admin.click("button:has-text('新增子文档')")
    admin.wait_for_url(req2_url)
    admin.fill("form[action$='/docs/new'] input[name=name]", "文档B")
    admin.click("button:has-text('新增子文档')")
    admin.wait_for_url(req2_url)
    assert "文档A" in admin.content() and "文档B" in admin.content()
    doc_a_link = admin.locator("table.docs tr:has-text('文档A') .copy").get_attribute("data-copy")

    # 公开页图片加载成功
    anon.goto(doc_a_link)
    anon.wait_for_load_state("load")
    assert anon.evaluate("() => { const i = document.getElementById('pic'); return i.complete && i.naturalWidth > 0; }")

    # 目录页列出子文档
    dir_url = share_url(admin)
    anon.goto(dir_url)
    assert "文档A" in anon.content() and "文档B" in anon.content()

    # 8. 单体转复合（页面内确认弹层）
    admin.goto(req1_url)
    admin.click("button:has-text('转为复合需求')")
    assert admin.locator("#confirm-modal").is_visible()
    confirm(admin)
    admin.wait_for_url(req1_url)
    assert "目录入口页" in admin.content() and "支付流程改版" in admin.content()

    # 9. 普通用户建需求 → 管理员删除 → 审计日志
    user.goto(f"{base_url}/req/new")
    user.fill("input[name=name]", "张三的需求")
    user.set_input_files("input[name=file]", {"name": "z.html", "mimeType": "text/html", "buffer": b"<p>z</p>"})
    user.click("button:has-text('创建')")
    user.wait_for_url(re.compile(r"/req/\d+$"))
    user_req_url = user.url
    # 普通用户看不到他人需求的删除按钮，也不能删版本
    user.goto(req1_url)
    assert user.locator("button:has-text('删除需求')").count() == 0
    assert user.locator("table.versions button:has-text('删除')").count() == 0
    admin.goto(user_req_url)
    admin.click("button:has-text('删除需求')")
    confirm(admin)
    admin.wait_for_url(f"{base_url}/")
    assert "已删除" in admin.content()
    assert user.goto(user_req_url).status == 404
    admin.goto(f"{base_url}/admin/audit")
    assert "delete_requirement" in admin.content() and "张三的需求" in admin.content()

    # 复制按钮存在且带完整链接
    admin.goto(f"{base_url}/")
    assert admin.locator(".copy").first.get_attribute("data-copy").startswith(base_url)

    for c in (admin_ctx, user_ctx, anon_ctx):
        c.close()
