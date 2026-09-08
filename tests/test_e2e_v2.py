"""2.0 端到端（Playwright）：导入快照 → 需求改稿 → 查看器中 iframe 圈出、高度自适应、改动定位、手动圈选、公开页。"""
from __future__ import annotations

import re
from urllib.parse import urlencode

import pytest

from tests.conftest import tg_params
from tests.test_e2e import live_url  # noqa: F401  复用同一台 uvicorn
from tests.test_v2 import snap_zip

pytestmark = pytest.mark.e2e

BASE_HTML = "<html><head><style>body{font:14px sans-serif;padding:20px}</style></head><body><h1 id='t'>商户列表</h1><table><tr><td>a</td><td>b</td></tr></table><button id='exp'>导出</button><div style='height:1600px'></div><p id='foot'>页脚</p></body></html>"
NEW_HTML = BASE_HTML.replace("<h1 id='t'>商户列表</h1>", "<h1 id='t'>商户列表（新）</h1><input id='q' placeholder='筛选'>").replace("<p id='foot'>页脚</p>", "<p id='foot'>页脚 v2</p>")


def test_v2_viewer_flow(browser, live_url):
    base = live_url
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    page.goto(f"{base}/auth/telegram?{urlencode(tg_params(1001, 'Super'))}")
    page.wait_for_url(f"{base}/")

    # 2.0 入口藏在系统设置页底部
    page.goto(f"{base}/admin/settings")
    page.click("text=DCPrd 2.0 预览")
    page.wait_for_url(f"{base}/v2/")

    # 建系统、端（若前面用例已建则复用）
    page.goto(f"{base}/v2/admin")
    if page.locator("text=E2E 体系").count() == 0:
        page.locator("form[action='/v2/admin/systems/new'] input[name=name]").fill("E2E 体系")
        page.locator("form[action='/v2/admin/systems/new'] button").click()
        page.wait_for_url(f"{base}/v2/admin")
    page.locator("form[action='/v2/admin/apps/new'] select[name=system_id]").select_option(label="EB · E2E 体系")
    page.locator("form[action='/v2/admin/apps/new'] input[name=name]").fill("E2E 后台")
    page.locator("form[action='/v2/admin/apps/new'] input[name=key]").fill("e2e-admin")
    page.locator("form[action='/v2/admin/apps/new'] button").click()
    page.wait_for_url(re.compile(r"/v2/admin/apps/\d+$"))
    app_url = page.url

    # 导入快照
    name, data, mime = snap_zip("1.0.0", {"/merchant/list": BASE_HTML, "/merchant/audit": "<h1>审核</h1>", "/settings": "<h1>设置</h1>"})
    page.set_input_files("form[action$='/snapshots/import'] input[name=file]", {"name": name, "mimeType": mime, "buffer": data})
    page.click("form[action$='/snapshots/import'] button")
    page.wait_for_url(app_url)
    assert "3 个新基线" in page.locator("#flash").inner_text()

    # 菜单视图：搜索过滤、打开页面
    page.click("text=浏览菜单视图")
    page.wait_for_url(re.compile(r"/v2/\?app=\d+"))
    page.fill("#tree-filter", "审核")
    assert page.locator(".mtree li:not(.hide) a.leaf", has_text="商户审核").count() == 1
    assert page.locator(".mtree li.hide", has_text="设置").count() == 1
    page.fill("#tree-filter", "")
    page.click("a.leaf:has-text('商户列表')")
    page.wait_for_url(re.compile(r"/v2/p/\d+"))
    page_url = page.url
    frame = page.frame_locator("#frame-new")
    assert frame.locator("#t").inner_text() == "商户列表"
    # iframe 高度自适应（内容 1600px+，默认 80vh=720px）
    page.wait_for_function("() => parseInt(document.getElementById('frame-new').style.height || '0') > 1500")

    # 发起改动 → 新建需求
    page.select_option("select[name=req_id]", "new")
    page.click("button:has-text('加入需求')")
    page.wait_for_url(re.compile(r"/v2/req/new"))
    page.fill("input[name=name]", "列表加筛选")
    page.fill("input[name=jira_keys]", "DC-2")
    page.click("button:has-text('创建')")
    page.wait_for_url(re.compile(r"/v2/req/\d+$"))
    req_url = page.url
    assert page.locator("text=下载基线").count() == 1

    # 上传改稿 → 跳到查看器
    page.set_input_files("form.upload-inline input[name=file]", {"name": "list.html", "mimeType": "text/html", "buffer": NEW_HTML.encode()})
    page.fill("form.upload-inline input[name=note]", "加筛选框")
    page.click("form.upload-inline button")
    page.wait_for_url(re.compile(r"/v2/p/\d+\?v=\d+"))
    assert "列表加筛选" in page.locator(".side").inner_text()
    # iframe 内已标记改动并画出角标
    frame = page.frame_locator("#frame-new")
    frame.locator("[data-dcpm-hit]").first.wait_for()
    hits = frame.locator("[data-dcpm-hit]").count()
    assert hits >= 3, hits  # h1 文字、新增 input、页脚文字
    assert frame.locator(".dcpm-badge").count() == hits
    assert frame.locator("#q[data-dcpm-hit='added']").count() == 1
    # 改动清单点击 → 子页面滚动到该元素并闪烁
    items = page.locator("#change-list li")
    assert items.count() == hits
    items.last.click()
    frame.locator("#foot.dcpm-flash").wait_for()
    # 关闭圈出后角标消失
    page.uncheck("#toggle-diff")
    page.wait_for_function("() => document.getElementById('frame-new').contentWindow !== null")
    assert frame.locator(".dcpm-badge:visible").count() == 0
    page.check("#toggle-diff")
    frame.locator(".dcpm-badge").first.wait_for()
    # 并排显示基线
    page.check("#toggle-split")
    assert page.locator(".stage.base").is_visible()
    assert page.frame_locator("#frame-base").locator("#t").inner_text() == "商户列表"

    # 手动圈选：拖出矩形，输入说明
    page.evaluate("window.scrollTo(0, 0)")
    page.uncheck("#toggle-split")
    logs = []
    page.on("console", lambda m: logs.append(f"console:{m.type}:{m.text}"))
    page.on("pageerror", lambda e: logs.append(f"pageerror:{e}"))
    page.once("dialog", lambda d: (logs.append(f"dialog:{d.type}:{d.message}"), d.accept("这里要加筛选")))
    page.click("#toggle-annotate")
    layer = page.locator("#ann-layer")
    box = layer.bounding_box()
    assert box["y"] >= 0, box
    logs.append("layer:" + str(layer.evaluate("l => { const r = l.getBoundingClientRect(); const e = document.elementFromPoint(r.left+40, r.top+40); return [l.className, getComputedStyle(l).pointerEvents, r.toJSON(), window.innerWidth, window.innerHeight, window.scrollY, e && (e.id || e.className || e.tagName)]; }")))
    page.mouse.move(box["x"] + 40, box["y"] + 40)
    page.mouse.down()
    page.mouse.move(box["x"] + 240, box["y"] + 140, steps=5)
    page.mouse.up()
    try:
        page.locator("#ann-list li", has_text="这里要加筛选").wait_for(timeout=8000)
    except Exception:
        logs.append("boxes:" + str(page.locator("#ann-layer .box").count()))
        raise AssertionError("\n".join(logs))
    assert page.locator("#ann-layer .box.manual").count() == 1
    page.reload()
    assert page.locator("#ann-layer .box.manual").count() == 1
    # 删除批注
    page.click("#ann-list .ann-del")
    page.wait_for_function("() => document.querySelectorAll('#ann-layer .box.manual').length === 0")

    # 时间线：两个节点（基线 + 需求）
    assert page.locator(".timeline .tl").count() == 2
    page.click(".timeline .tl.baseline")
    assert page.frame_locator("#frame-new").locator("#t").inner_text() == "商户列表"

    # 公开分享页（免登录）
    page.goto(req_url)
    share = page.locator(".share-row input").first.input_value()
    anon = browser.new_context().new_page()
    anon.goto(share)
    assert "列表加筛选" in anon.content() and "商户列表" in anon.content()
    anon.click("a:has-text('商户列表')")
    anon.wait_for_url(re.compile(r"/p2/[a-z0-9]{12}/\d+/"))
    f2 = anon.frame_locator("#frame-new")
    f2.locator("[data-dcpm-hit]").first.wait_for()
    assert f2.locator(".dcpm-badge").count() >= 3
    assert anon.locator("#toggle-annotate").count() == 0
    assert anon.goto(f"{base}/v2/").url.startswith(f"{base}/login")
    ctx.close()
