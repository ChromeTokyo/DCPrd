#!/usr/bin/env python3
"""dcpm-snap：把一个后台 / Flutter Web 端按菜单逐页快照，打包成 DCPrd 2.0 可导入的 zip。

用法：
  pip install playwright && python -m playwright install chromium
  python snap.py --config eb-ops.json                # 生成 snapshot-<app>-<release>.zip
  python snap.py --config eb-ops.json --login        # 打开有界面的浏览器手动登录，保存登录态到 state.json 后退出
  python snap.py --config eb-ops.json --only /merchant   # 只抓某个路由前缀（调试用）
  python snap.py --config eb-ops.json --limit 5      # 只抓前 5 页（调试用）

配置文件见 README.md / example-config.json。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

TITLE_KEYS = ("title", "name", "label", "text")
PATH_KEYS = ("route", "path", "key", "url", "fullPath")
CHILD_KEYS = ("children", "routes", "items", "subMenu")
MAX_ASSET = 6 * 1024 * 1024


# ---------- 菜单规整（与服务端 normalize_menu 保持一致） ----------

def _pick(d: dict, keys, nested_meta=True) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    if nested_meta and isinstance(d.get("meta"), dict):
        return _pick(d["meta"], keys, False)
    return ""


def normalize_menu(raw, parent_path=""):
    if isinstance(raw, dict):
        for k in CHILD_KEYS + ("menu", "data"):
            if isinstance(raw.get(k), list):
                return normalize_menu(raw[k], parent_path)
        raw = [raw]
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        title = _pick(item, TITLE_KEYS)
        route = _pick(item, PATH_KEYS, False)
        if route and not route.startswith(("/", "#", "http")) and parent_path:
            route = parent_path.rstrip("/") + "/" + route
        children = []
        for k in CHILD_KEYS:
            if isinstance(item.get(k), list):
                children = normalize_menu(item[k], route or parent_path)
                break
        hidden = item.get("hidden") is True or (isinstance(item.get("meta"), dict) and item["meta"].get("hidden") is True)
        if hidden or (not title and not children):
            continue
        out.append({"title": title or route or "(未命名)", "route": route, "children": children})
    return out


def leaves(menu, acc=None):
    acc = [] if acc is None else acc
    for it in menu:
        if it["children"]:
            leaves(it["children"], acc)
        elif it["route"]:
            acc.append(it)
    return acc


# ---------- HTML 冻结（在页面内执行） ----------

FREEZE_PREPARE_JS = r"""
(cfg) => {
  // 表单值写回属性、canvas 转图片、移除干扰元素、遮罩动态区域
  document.querySelectorAll('input').forEach(i => {
    if (i.type === 'checkbox' || i.type === 'radio') { if (i.checked) i.setAttribute('checked', ''); else i.removeAttribute('checked'); }
    else if (i.type !== 'password' && i.type !== 'file') i.setAttribute('value', i.value);
  });
  document.querySelectorAll('textarea').forEach(t => { t.textContent = t.value; });
  document.querySelectorAll('select').forEach(s => { Array.from(s.options).forEach(o => { if (o.selected) o.setAttribute('selected', ''); else o.removeAttribute('selected'); }); });
  document.querySelectorAll('canvas').forEach(c => { try { const img = document.createElement('img'); img.src = c.toDataURL('image/png'); img.width = c.width; img.height = c.height; img.className = c.className; img.setAttribute('style', c.getAttribute('style') || ''); c.replaceWith(img); } catch (e) {} });
  (cfg.remove || []).forEach(sel => { try { document.querySelectorAll(sel).forEach(el => el.remove()); } catch (e) {} });
  (cfg.mask || []).forEach(sel => { try { document.querySelectorAll(sel).forEach(el => { el.setAttribute('data-dcpm-masked', '1'); if (el.children.length === 0) el.textContent = '—'; }); } catch (e) {} });
  const urls = new Set();
  document.querySelectorAll('link[rel~="stylesheet"][href]').forEach(l => urls.add(l.href));
  document.querySelectorAll('img[src], source[src], video[poster]').forEach(el => { const u = el.src || el.getAttribute('poster'); if (u && !u.startsWith('data:')) urls.add(u); });
  document.querySelectorAll('[style*="url("]').forEach(el => { const m = el.getAttribute('style').match(/url\((['"]?)([^'")]+)\1\)/g) || []; m.forEach(x => { const u = x.replace(/^url\((['"]?)/, '').replace(/(['"]?)\)$/, ''); if (!u.startsWith('data:')) urls.add(new URL(u, location.href).href); }); });
  return Array.from(urls);
}
"""

FREEZE_SERIALIZE_JS = r"""
(args) => {
  const map = args.map;  // url -> data url 或 {css: text}
  const doc = document.cloneNode(true);
  doc.querySelectorAll('script, noscript, link[rel="preload"], link[rel="prefetch"], link[rel="modulepreload"]').forEach(el => el.remove());
  doc.querySelectorAll('iframe').forEach(f => { const d = doc.createElement('div'); d.setAttribute('data-dcpm-iframe', f.getAttribute('src') || ''); d.style.cssText = 'border:1px dashed #999;padding:8px;color:#666;font-size:12px'; d.textContent = '[iframe] ' + (f.getAttribute('src') || ''); f.replaceWith(d); });
  doc.querySelectorAll('link[rel~="stylesheet"][href]').forEach(l => {
    const abs = new URL(l.getAttribute('href'), location.href).href;
    const css = map[abs];
    if (css && css.css !== undefined) { const s = doc.createElement('style'); s.setAttribute('data-dcpm-inlined', abs); s.textContent = css.css; l.replaceWith(s); }
  });
  doc.querySelectorAll('img[src], source[src], video[poster]').forEach(el => {
    const attr = el.hasAttribute('poster') ? 'poster' : 'src';
    const abs = new URL(el.getAttribute(attr), location.href).href;
    if (map[abs] && typeof map[abs] === 'string') { el.setAttribute(attr, map[abs]); el.removeAttribute('srcset'); el.removeAttribute('loading'); }
  });
  doc.querySelectorAll('[style*="url("]').forEach(el => {
    let st = el.getAttribute('style');
    st = st.replace(/url\((['"]?)([^'")]+)\1\)/g, (m, q, u) => { const abs = new URL(u, location.href).href; return (map[abs] && typeof map[abs] === 'string') ? 'url(' + map[abs] + ')' : m; });
    el.setAttribute('style', st);
  });
  if (!doc.querySelector('meta[charset]')) { const m = doc.createElement('meta'); m.setAttribute('charset', 'utf-8'); doc.head.insertBefore(m, doc.head.firstChild); }
  const meta = doc.createElement('meta'); meta.name = 'dcpm-snapshot'; meta.content = args.info; doc.head.appendChild(meta);
  return '<!DOCTYPE html>\n' + doc.documentElement.outerHTML;
}
"""

MASK_STYLE = "[data-dcpm-masked]{background:#e5e7eb !important;color:transparent !important;}"
CSS_URL_RE = re.compile(r"url\((['\"]?)([^'\")]+)\1\)")
CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\()?['\"]?([^'\")]+)['\"]?\)?[^;]*;")


class Snapper:
    def __init__(self, cfg: dict, args):
        self.cfg = cfg
        self.args = args
        self.asset_cache: dict[str, str | dict] = {}
        self.report = {"ok": [], "failed": []}

    # --- 资源 ---
    def fetch(self, page, url: str) -> bytes | None:
        try:
            r = page.request.get(url, timeout=20000)
            if not r.ok:
                return None
            body = r.body()
            return body if len(body) <= MAX_ASSET else None
        except PWError:
            return None

    def data_url(self, url: str, body: bytes) -> str:
        mime = mimetypes.guess_type(urlparse(url).path)[0] or "application/octet-stream"
        return f"data:{mime};base64,{base64.b64encode(body).decode()}"

    def inline_css(self, page, css_url: str, depth=0) -> str:
        body = self.fetch(page, css_url)
        if body is None:
            return f"/* dcpm: failed to fetch {css_url} */"
        text = body.decode("utf-8", errors="replace")
        if depth < 2:
            def imp(m):
                sub = urljoin(css_url, m.group(1))
                return self.inline_css(page, sub, depth + 1)
            text = CSS_IMPORT_RE.sub(imp, text)

        def rep(m):
            u = m.group(2)
            if u.startswith(("data:", "#")):
                return m.group(0)
            abs_u = urljoin(css_url, u)
            if abs_u not in self.asset_cache:
                b = self.fetch(page, abs_u)
                self.asset_cache[abs_u] = self.data_url(abs_u, b) if b is not None else ""
            d = self.asset_cache[abs_u]
            return f"url({d})" if d else m.group(0)
        return CSS_URL_RE.sub(rep, text)

    # --- 页面 ---
    def goto(self, page, route: str):
        tpl = self.cfg.get("route_template") or ("{base_url}/#{route}" if self.cfg.get("hash_routing") else "{base_url}{route}")
        url = tpl.format(base_url=self.cfg["base_url"].rstrip("/"), route=route)
        page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.get("timeout_ms", 45000))
        wait = self.cfg.get("wait") or {}
        if wait.get("network_idle", True):
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWError:
                pass
        if wait.get("selector"):
            try:
                page.wait_for_selector(wait["selector"], timeout=wait.get("timeout_ms", 10000))
            except PWError:
                pass
        page.wait_for_timeout(wait.get("ms", 1200))
        return url

    def capture_html(self, page) -> str:
        urls = page.evaluate(FREEZE_PREPARE_JS, {"remove": self.cfg.get("remove", []), "mask": self.cfg.get("mask", [])})
        mapping: dict = {}
        for u in urls:
            if u in self.asset_cache:
                mapping[u] = self.asset_cache[u]
                continue
            if re.search(r"\.css(\?|$)", urlparse(u).path + ("?" if "?" in u else "")) or "text/css" in u:
                val: str | dict = {"css": self.inline_css(page, u)}
            else:
                b = self.fetch(page, u)
                val = self.data_url(u, b) if b is not None else ""
            self.asset_cache[u] = val
            mapping[u] = val
        # link 标签即使不以 .css 结尾也按 CSS 处理
        for u in urls:
            if isinstance(mapping.get(u), str) and page.evaluate("(u) => !!document.querySelector('link[rel~=\"stylesheet\"][href]') && Array.from(document.querySelectorAll('link[rel~=\"stylesheet\"]')).some(l => l.href === u)", u):
                mapping[u] = {"css": self.inline_css(page, u)}
        info = json.dumps({"app": self.cfg["app"], "release": self.cfg["release"], "url": page.url, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False)
        html = page.evaluate(FREEZE_SERIALIZE_JS, {"map": mapping, "info": info})
        if self.cfg.get("mask"):
            html = html.replace("</head>", f"<style>{MASK_STYLE}</style></head>", 1)
        return html

    def capture_image(self, page) -> bytes:
        if self.cfg.get("mask") or self.cfg.get("remove"):
            page.evaluate(FREEZE_PREPARE_JS, {"remove": self.cfg.get("remove", []), "mask": self.cfg.get("mask", [])})
            page.add_style_tag(content=MASK_STYLE)
        if self.cfg.get("kind") == "flutter_web" or self.cfg.get("wait_flutter"):
            try:
                page.wait_for_selector("flt-glass-pane, flutter-view, canvas", timeout=10000)
            except PWError:
                pass
            page.wait_for_timeout(self.cfg.get("flutter_extra_ms", 1500))
        return page.screenshot(full_page=self.cfg.get("full_page", True), type="png", animations="disabled", caret="hide")

    # --- 登录 ---
    def login(self, context, page):
        lg = self.cfg.get("login") or {}
        state_file = Path(lg.get("storage_state") or "state.json")
        if lg.get("type") == "form":
            page.goto(urljoin(self.cfg["base_url"], lg.get("url", "/login")), wait_until="domcontentloaded")
            user = os.environ.get(lg.get("user_env", "SNAP_USER"), lg.get("user", ""))
            pwd = os.environ.get(lg.get("pass_env", "SNAP_PASS"), lg.get("password", ""))
            page.fill(lg["user_selector"], user)
            page.fill(lg["pass_selector"], pwd)
            page.click(lg["submit_selector"])
            page.wait_for_timeout(lg.get("wait_after_ms", 3000))
            if lg.get("success_selector"):
                page.wait_for_selector(lg["success_selector"], timeout=20000)
            context.storage_state(path=str(state_file))
            print(f"[login] 表单登录完成，登录态已保存到 {state_file}")
        elif lg.get("type") in ("storage_state", None):
            if not state_file.exists():
                print(f"[login] 未找到 {state_file}，请先执行 --login 手动登录", file=sys.stderr)
        return

    def manual_login(self):
        lg = self.cfg.get("login") or {}
        state_file = Path(lg.get("storage_state") or "state.json")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(viewport=self.cfg.get("viewport") or {"width": 1440, "height": 900})
            page = context.new_page()
            page.goto(urljoin(self.cfg["base_url"], lg.get("url", "/")), wait_until="domcontentloaded")
            input("请在打开的浏览器里完成登录，进入后台首页后回到这里按回车…")
            context.storage_state(path=str(state_file))
            print(f"登录态已保存到 {state_file}，之后直接运行 snap.py 即可。")
            browser.close()

    # --- 主流程 ---
    def run(self) -> Path:
        cfg = self.cfg
        menu_src = cfg.get("menu")
        if menu_src is None and cfg.get("routes"):
            menu_src = json.loads(Path(cfg["routes"]).read_text("utf-8"))
        menu = normalize_menu(menu_src or [])
        pages = leaves(menu)
        seen = set()
        pages = [p for p in pages if not (p["route"] in seen or seen.add(p["route"]))]
        only = self.args.only or cfg.get("only") or []
        only = [only] if isinstance(only, str) else only
        exclude = cfg.get("exclude") or []
        if only:
            pages = [p for p in pages if any(p["route"].startswith(x) for x in only)]
        if exclude:
            pages = [p for p in pages if not any(p["route"].startswith(x) for x in exclude)]
        if self.args.limit:
            pages = pages[: self.args.limit]
        if not pages:
            sys.exit("没有可抓取的页面：请检查 routes/menu 配置")
        renderer = "image" if cfg.get("renderer") in ("image", "screenshot") or cfg.get("kind") == "flutter_web" else "html"
        out = Path(self.args.output or cfg.get("output") or f"snapshot-{cfg['app']}-{cfg['release']}.zip")
        print(f"[snap] app={cfg['app']} release={cfg['release']} renderer={renderer} pages={len(pages)} -> {out}")

        lg = cfg.get("login") or {}
        state_file = Path(lg.get("storage_state") or "state.json")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not self.args.headful)
            ctx_kw = {"viewport": cfg.get("viewport") or {"width": 1440, "height": 900}, "device_scale_factor": cfg.get("device_scale_factor", 1), "locale": cfg.get("locale", "zh-CN")}
            if lg.get("type") in ("storage_state", None) and state_file.exists():
                ctx_kw["storage_state"] = str(state_file)
            if cfg.get("user_agent"):
                ctx_kw["user_agent"] = cfg["user_agent"]
            context = browser.new_context(**ctx_kw)
            context.set_default_timeout(cfg.get("timeout_ms", 45000))
            page = context.new_page()
            if lg.get("type") == "form":
                self.login(context, page)

            manifest_pages = []
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, pg in enumerate(pages, 1):
                    route = pg["route"]
                    fname = f"pages/{i:04d}-{re.sub(r'[^A-Za-z0-9_-]+', '_', route).strip('_')[:60] or 'index'}.{'png' if renderer == 'image' else 'html'}"
                    for attempt in range(1, cfg.get("retries", 2) + 2):
                        try:
                            url = self.goto(page, route)
                            if cfg.get("login_check_selector") and page.query_selector(cfg["login_check_selector"]):
                                raise RuntimeError("检测到登录页，登录态可能已失效，请重新 --login")
                            data = self.capture_image(page) if renderer == "image" else self.capture_html(page).encode("utf-8")
                            zf.writestr(fname, data)
                            manifest_pages.append({"route": route, "title": pg["title"], "file": fname, "hash": hashlib.sha256(data).hexdigest(), "url": url, "size": len(data)})
                            self.report["ok"].append(route)
                            print(f"  [{i}/{len(pages)}] {route}  {len(data)//1024} KB")
                            break
                        except Exception as e:  # noqa: BLE001
                            if attempt > cfg.get("retries", 2):
                                self.report["failed"].append({"route": route, "error": str(e)[:300]})
                                print(f"  [{i}/{len(pages)}] {route}  失败：{str(e)[:120]}", file=sys.stderr)
                            else:
                                page.wait_for_timeout(1000)
                manifest = {
                    "app": cfg["app"], "release": cfg["release"], "renderer": renderer, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "base_url": cfg["base_url"], "viewport": ctx_kw["viewport"], "menu": menu, "pages": manifest_pages, "failed": self.report["failed"],
                }
                zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
                zf.writestr("report.json", json.dumps(self.report, ensure_ascii=False, indent=1))
            browser.close()
        print(f"[snap] 完成：成功 {len(self.report['ok'])}，失败 {len(self.report['failed'])}，输出 {out}")
        return out


def main():
    ap = argparse.ArgumentParser(description="DCPrd 2.0 页面快照工具")
    ap.add_argument("--config", required=True, help="配置 JSON")
    ap.add_argument("--login", action="store_true", help="打开浏览器手动登录并保存登录态")
    ap.add_argument("--headful", action="store_true", help="抓取时显示浏览器窗口")
    ap.add_argument("--only", action="append", help="只抓此路由前缀（可多次）")
    ap.add_argument("--limit", type=int, help="只抓前 N 页")
    ap.add_argument("--output", help="输出 zip 路径")
    ap.add_argument("--release", help="覆盖配置中的 release")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text("utf-8"))
    if args.release:
        cfg["release"] = args.release
    for k in ("app", "release", "base_url"):
        if not cfg.get(k):
            sys.exit(f"配置缺少 {k}")
    s = Snapper(cfg, args)
    if args.login:
        s.manual_login()
        return
    s.run()


if __name__ == "__main__":
    main()
