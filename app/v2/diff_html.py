"""HTML DOM diff：找出两版页面的差异节点，输出可在浏览器中定位的 CSS 路径，并把标注脚本注入到目标 HTML。"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field

import html5lib

MAX_CHANGES = 400
IGNORE_TAGS = {"script", "style", "noscript", "template", "head", "meta", "link", "title"}
IGNORE_ATTR_PREFIX = "data-dcpm"
_WS = re.compile(r"\s+")


@dataclass
class Node:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)   # Node
    text: str = ""                                  # 直接文本（合并空白）
    index: int = 1                                  # nth-of-type
    parent: "Node | None" = None

    def path(self) -> str:
        parts = []
        n = self
        while n is not None:
            parts.append(f"{n.tag}:nth-of-type({n.index})" if n.parent is not None else n.tag)
            n = n.parent
        return ">".join(reversed(parts))

    def label(self) -> str:
        t = self.text.strip()
        if t:
            return f"<{self.tag}> {t[:40]}"
        for k in ("aria-label", "placeholder", "title", "alt", "name", "id"):
            if self.attrs.get(k):
                return f"<{self.tag}> {self.attrs[k][:40]}"
        return f"<{self.tag}>"


def _norm_text(s: str | None) -> str:
    return _WS.sub(" ", s or "").strip()


def _clean_attrs(attrs: dict) -> dict:
    out = {}
    for k, v in attrs.items():
        k = k if isinstance(k, str) else k[1]
        if k.startswith(IGNORE_ATTR_PREFIX):
            continue
        if k == "class":
            v = " ".join(sorted(v.split()))
        out[k] = v
    return out


def _build(el, parent: Node | None, counters: dict) -> Node | None:
    tag = el.tag if isinstance(el.tag, str) else ""
    if "}" in tag:
        tag = tag.split("}", 1)[1]
    tag = tag.lower()
    if not tag or tag in IGNORE_TAGS:
        return None
    node = Node(tag=tag, attrs=_clean_attrs(el.attrib), parent=parent)
    if parent is not None:
        counters[tag] = counters.get(tag, 0) + 1
        node.index = counters[tag]
    texts = [_norm_text(el.text)]
    child_counters: dict = {}
    for ch in el:
        # 跳过被忽略的标签但仍要为浏览器 nth-of-type 计数（浏览器同样计数 script/style 等）
        ctag = (ch.tag.split("}", 1)[1] if isinstance(ch.tag, str) and "}" in ch.tag else ch.tag)
        ctag = ctag.lower() if isinstance(ctag, str) else ""
        if ctag in IGNORE_TAGS:
            child_counters[ctag] = child_counters.get(ctag, 0) + 1
        else:
            c = _build(ch, node, child_counters)
            if c is not None:
                node.children.append(c)
        texts.append(_norm_text(ch.tail))
    node.text = _norm_text(" ".join(t for t in texts if t))
    return node


def parse_tree(html_text: str) -> Node:
    doc = html5lib.parse(html_text, treebuilder="etree", namespaceHTMLElements=False)
    root = _build(doc, None, {})
    assert root is not None
    return root


def _sig(n: Node) -> str:
    """节点签名：有 id/name 的用标识；叶子节点带上文字，使表格插列、列表插项能按内容对齐。"""
    ident = n.attrs.get("id") or n.attrs.get("data-key") or n.attrs.get("name") or ""
    sig = f"{n.tag}#{ident}|{n.attrs.get('class', '')}"
    if not n.children and not ident:
        sig += "|" + n.text[:80]
    return sig


def _content_hash(n: Node) -> str:
    h = hashlib.md5()
    stack = [n]
    while stack:
        x = stack.pop()
        h.update(x.tag.encode()); h.update(x.text.encode("utf-8", "ignore"))
        h.update(json.dumps(x.attrs, sort_keys=True, ensure_ascii=False).encode("utf-8", "ignore"))
        stack.extend(x.children)
    return h.hexdigest()


def _count(n: Node) -> int:
    return 1 + sum(_count(c) for c in n.children)


class Differ:
    def __init__(self):
        self.changes: list[dict] = []

    def add(self, typ: str, node: Node, **extra):
        if len(self.changes) >= MAX_CHANGES:
            return
        self.changes.append({"type": typ, "path": node.path(), "label": node.label(), **extra})

    def diff(self, a: Node, b: Node) -> None:
        if _content_hash(a) == _content_hash(b):
            return
        if a.attrs != b.attrs:
            changed = sorted({k for k in set(a.attrs) | set(b.attrs) if a.attrs.get(k) != b.attrs.get(k)})
            self.add("attr", b, attrs=changed[:8], old={k: a.attrs.get(k, "") for k in changed[:8]}, new={k: b.attrs.get(k, "") for k in changed[:8]})
        if a.text != b.text:
            self.add("text", b, old=a.text[:200], new=b.text[:200])
        sa, sb = [_sig(c) for c in a.children], [_sig(c) for c in b.children]
        sm = difflib.SequenceMatcher(a=sa, b=sb, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    self.diff(a.children[i], b.children[j])
            elif op == "replace":
                # 同标签按位配对递归，其余按增删
                ac, bc = a.children[i1:i2], b.children[j1:j2]
                used = set()
                for bn in bc:
                    match = next((k for k, an in enumerate(ac) if k not in used and an.tag == bn.tag), None)
                    if match is None:
                        self.add("added", bn)
                    else:
                        used.add(match)
                        self.diff(ac[match], bn)
                for k, an in enumerate(ac):
                    if k not in used:
                        self.add("removed", b, removed=an.label())
            elif op == "delete":
                for an in a.children[i1:i2]:
                    self.add("removed", b, removed=an.label())
            elif op == "insert":
                for bn in b.children[j1:j2]:
                    self.add("added", bn)


def diff_html(base_html: str, new_html: str) -> dict:
    """返回 {"changes": [...], "total": 节点数, "ratio": 变更占比, "counts": {...}}。"""
    a, b = parse_tree(base_html), parse_tree(new_html)
    d = Differ()
    d.diff(a, b)
    total = max(1, _count(b))
    counts: dict[str, int] = {}
    for c in d.changes:
        counts[c["type"]] = counts.get(c["type"], 0) + 1
    return {"changes": d.changes, "total": total, "ratio": round(min(1.0, len(d.changes) / total), 3), "counts": counts, "truncated": len(d.changes) >= MAX_CHANGES}


DIFF_STYLE = """
[data-dcpm-hit]{outline:2px solid #ef4444 !important;outline-offset:2px;position:relative}
[data-dcpm-hit="removed"]{outline-style:dashed !important;outline-color:#f59e0b !important}
[data-dcpm-hit="added"]{outline-color:#16a34a !important}
.dcpm-badge{position:absolute;z-index:2147483000;background:#ef4444;color:#fff;font:bold 11px/1 sans-serif;padding:3px 5px;border-radius:10px;pointer-events:none;transform:translate(-50%,-50%);box-shadow:0 1px 3px rgba(0,0,0,.3)}
.dcpm-badge.added{background:#16a34a}.dcpm-badge.removed{background:#f59e0b}
.dcpm-flash{animation:dcpmflash 1.2s ease-out 2}
@keyframes dcpmflash{0%{box-shadow:0 0 0 6px rgba(239,68,68,.6)}100%{box-shadow:0 0 0 0 rgba(239,68,68,0)}}
"""

DIFF_SCRIPT = """
(function(){
  var changes = __CHANGES__;
  var els = [];
  function place(){
    document.querySelectorAll('.dcpm-badge').forEach(function(b){b.remove();});
    els.forEach(function(el, i){
      if(!el) return;
      var r = el.getBoundingClientRect();
      var b = document.createElement('div');
      b.className = 'dcpm-badge ' + (changes[i].type||'');
      b.textContent = String(i+1);
      b.style.left = (r.left + window.scrollX) + 'px';
      b.style.top = (r.top + window.scrollY) + 'px';
      document.body.appendChild(b);
    });
  }
  function init(){
    els = changes.map(function(c){
      var el = null;
      try { el = document.querySelector(c.path); } catch(e){}
      if(el && el.tagName && el.tagName.toLowerCase()!=='html' && el.tagName.toLowerCase()!=='body') el.setAttribute('data-dcpm-hit', c.type);
      return el;
    });
    place();
    var found = els.filter(Boolean).length;
    try { window.parent.postMessage({type:'dcpm-ready', total: changes.length, found: found}, '*'); } catch(e){}
  }
  window.addEventListener('message', function(ev){
    var d = ev.data || {};
    if(d.type === 'dcpm-focus'){
      var el = els[d.index];
      if(el){ el.scrollIntoView({block:'center', behavior:'smooth'}); el.classList.remove('dcpm-flash'); void el.offsetWidth; el.classList.add('dcpm-flash'); }
    } else if (d.type === 'dcpm-toggle'){
      document.documentElement.classList.toggle('dcpm-hide', !d.on);
      document.querySelectorAll('.dcpm-badge').forEach(function(b){ b.style.display = d.on ? '' : 'none'; });
      if(!d.on){ els.forEach(function(el){ if(el) el.removeAttribute('data-dcpm-hit'); }); }
      else { els.forEach(function(el,i){ if(el) el.setAttribute('data-dcpm-hit', changes[i].type); }); place(); }
    }
  });
  var lastH = 0;
  function contentHeight(){
    var b = document.body; if(!b) return 0;
    var h = b.offsetHeight + (parseFloat(getComputedStyle(b).marginTop)||0) + (parseFloat(getComputedStyle(b).marginBottom)||0);
    for (var i = 0; i < b.children.length; i++) { var r = b.children[i].getBoundingClientRect(); if (r.height) h = Math.max(h, r.bottom + window.scrollY); }
    return Math.ceil(h);
  }
  function reportSize(){
    var h = contentHeight();
    if(h && Math.abs(h - lastH) > 8){ lastH = h; try { window.parent.postMessage({type:'dcpm-size', height: h}, '*'); } catch(e){} }
  }
  window.addEventListener('resize', function(){ place(); reportSize(); });
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  setTimeout(function(){ place(); reportSize(); }, 300);
  setTimeout(reportSize, 1500);
  try { new MutationObserver(function(){ reportSize(); }).observe(document.documentElement, {childList:true, subtree:true, attributes:true}); } catch(e){}
  reportSize();
})();
"""


def inject_diff(html_text: str, changes: list[dict]) -> str:
    """把标注样式与脚本追加到 HTML 末尾（不改动原有节点，保证 nth-of-type 路径不变）。"""
    payload = json.dumps([{"path": c["path"], "type": c["type"]} for c in changes], ensure_ascii=False).replace("</", "<\\/")
    block = f'<style data-dcpm="1">{DIFF_STYLE}</style><script data-dcpm="1">{DIFF_SCRIPT.replace("__CHANGES__", payload)}</script>'
    idx = html_text.lower().rfind("</body>")
    if idx == -1:
        return html_text + block
    return html_text[:idx] + block + html_text[idx:]
