"""公开 HTML 页面右下角的需求信息小菜单：注入到入口页（仅入口页，不注入 zip 内其他资源）。

注入片段全部为 ASCII（非 ASCII 走 \\uXXXX 转义），因此对 gbk 等任意页面编码都安全；样式用 Shadow DOM 隔离。
"""
from __future__ import annotations

import json
import re

_BODY_END = re.compile(rb"</body\s*>", re.I)

WIDGET_JS = r"""
(function(){
  var D = __DATA__;
  var T = {info:'需求信息', owners:'负责人', updated:'更新时间', versions:'历史版本', latest:'最新', current:'当前', history:'历史版本', dir:'返回目录', close:'收起', none:'—', viewingOld:'你正在查看历史版本，', toLatest:'前往最新版', by:'', jira:'Jira', tabInfo:'信息', tabComments:'留言', download:'打包下载', file:'原文件', yourName:'你的名字（必填）', yourComment:'留言内容：问题、建议或确认', send:'发送', sending:'发送中…', noComments:'还没有留言，欢迎第一个提出。', resolved:'已处理', loadFail:'留言加载失败', needBoth:'请填写名字和留言内容'};
  function el(tag, attrs, children){ var e = document.createElement(tag); for (var k in (attrs||{})) { if (k === 'text') e.textContent = attrs[k]; else if (k === 'html') e.innerHTML = attrs[k]; else e.setAttribute(k, attrs[k]); } (children||[]).forEach(function(c){ if (c) e.appendChild(c); }); return e; }
  var host = el('div', {id:'dcpm-widget-host'});
  host.style.cssText = 'all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483646;';
  var root = host.attachShadow ? host.attachShadow({mode:'open'}) : host;
  var css = '.pill{display:flex;align-items:center;gap:8px;background:#111827;color:#fff;font:13px/1 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;padding:9px 14px;border-radius:999px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);max-width:60vw;user-select:none}' +
    '.pill .v{background:#2563eb;border-radius:999px;padding:2px 7px;font-size:11px;font-weight:600}.pill .v.old{background:#d97706}.pill .n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.panel{display:none;width:340px;max-width:calc(100vw - 32px);max-height:70vh;overflow:auto;background:#fff;color:#1f2329;border:1px solid #e5e7eb;border-radius:12px;box-shadow:0 16px 40px rgba(0,0,0,.22);font:13px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}' +
    '.open .panel{display:block}.open .pill{display:none}' +
    '.hd{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;padding:12px 14px 8px;border-bottom:1px solid #f1f3f5}.hd h3{margin:0;font-size:15px;line-height:1.4}.hd .doc{color:#6b7280;font-size:12px;margin-top:2px}' +
    '.x{border:0;background:#f3f4f6;color:#374151;border-radius:6px;padding:4px 8px;font-size:12px;cursor:pointer;flex:none}.x:hover{background:#e5e7eb}' +
    '.tag{display:inline-block;font-size:10px;line-height:1;padding:3px 5px;border-radius:4px;color:#fff;font-weight:600;vertical-align:middle;margin-right:4px}.tag-eb{background:#0ea5e9}.tag-im{background:#8b5cf6}.tag-tk{background:#f59e0b}' +
    'dl{display:grid;grid-template-columns:64px 1fr;gap:4px 10px;margin:0;padding:10px 14px;border-bottom:1px solid #f1f3f5}dt{color:#6b7280}dd{margin:0;word-break:break-word}' +
    '.warn{background:#fef3c7;color:#92400e;padding:8px 14px;font-size:12px}.warn a{color:#92400e;font-weight:600}' +
    '.vs{list-style:none;margin:0;padding:6px 8px 10px}.vs li{display:flex;align-items:stretch}.vs li a{display:flex;gap:8px;align-items:baseline;padding:6px 8px;border-radius:6px;color:#1f2329;text-decoration:none;flex:1;min-width:0}.vs li a:hover{background:#f3f4f6}.vs li.cur a{background:#eef2ff}.vs li a.file{flex:none;color:#6b7280;padding:6px 8px;font-weight:700}.vs li a.file:hover{color:#2563eb}' +
    '.vs .num{font-weight:600;min-width:34px}.vs .meta{color:#6b7280;font-size:12px;flex:1}.vs .note{display:block;color:#374151;font-size:12px}.vs .cur-badge{font-size:10px;background:#2563eb;color:#fff;border-radius:999px;padding:1px 6px}' +
    '.ft{padding:8px 14px 12px;display:flex;gap:8px;flex-wrap:wrap}.btn{display:inline-block;padding:5px 10px;border:1px solid #d1d5db;border-radius:6px;color:#374151;text-decoration:none;font-size:12px;background:#fff}.btn.primary{background:#2563eb;border-color:#2563eb;color:#fff}' +
    '.jira{font-family:ui-monospace,Menlo,monospace;font-size:12px;margin-right:6px;color:#2563eb;text-decoration:none}' +
    '.tabs{display:flex;border-bottom:1px solid #f1f3f5}.tabs button{flex:1;border:0;background:none;padding:8px;font:inherit;font-size:13px;color:#6b7280;cursor:pointer;border-bottom:2px solid transparent}.tabs button.on{color:#2563eb;border-bottom-color:#2563eb;font-weight:600}' +
    '.sec{display:none}.sec.on{display:block}' +
    '.cl{list-style:none;margin:0;padding:6px 14px;max-height:34vh;overflow:auto}.cl li{padding:8px 0;border-bottom:1px solid #f1f3f5}.cl li:last-child{border-bottom:0}.cl .a{font-weight:600}.cl .t{color:#6b7280;font-size:11px;margin-left:6px}.cl .b{white-space:pre-wrap;margin-top:2px}.cl .r{font-size:10px;background:#dcfce7;color:#166534;border-radius:999px;padding:1px 6px;margin-left:6px}.cl .empty{color:#6b7280;padding:10px 0}' +
    '.cf{padding:10px 14px 12px;border-top:1px solid #f1f3f5;display:flex;flex-direction:column;gap:6px}.cf input,.cf textarea{font:inherit;font-size:13px;border:1px solid #d1d5db;border-radius:6px;padding:6px 8px;width:100%;box-sizing:border-box}.cf textarea{min-height:64px;resize:vertical}.cf .row{display:flex;justify-content:space-between;align-items:center;gap:8px}.cf .err{color:#991b1b;font-size:12px}.cf .ok{color:#166534;font-size:12px}' +
    '.cf button{border:0;background:#2563eb;color:#fff;border-radius:6px;padding:6px 14px;font:inherit;font-size:13px;cursor:pointer}.cf button[disabled]{opacity:.6}.badge{background:#ef4444;color:#fff;border-radius:999px;font-size:10px;padding:1px 6px;margin-left:4px}';
  var isOld = D.current !== D.latest;
  var wrap = el('div', {class:'wrap'});
  var pill = el('div', {class:'pill', title:T.info}, [ el('span', {class:'v' + (isOld ? ' old' : ''), text:'v' + D.current + (isOld ? ' ' + T.history : '')}), el('span', {class:'n', text: D.doc.compound ? (D.req.name + ' · ' + D.doc.name) : D.req.name}) ]);
  var hd = el('div', {class:'hd'}, [ el('div', {}, [ el('h3', {}, [ el('span', {class:'tag tag-' + D.req.project, text:D.req.projectLabel}), document.createTextNode(D.req.name) ]), D.doc.compound ? el('div', {class:'doc', text:D.doc.name}) : null ]), el('button', {class:'x', type:'button', text:T.close}) ]);
  var jira = el('span', {}, D.req.jira.map(function(j){ return el('a', {class:'jira', href:j.url, target:'_blank', rel:'noopener', text:j.key}); }));
  var dl = el('dl', {}, [
    el('dt', {text:T.owners}), el('dd', {text: D.req.owners.length ? D.req.owners.join('、') : T.none}),
    el('dt', {text:T.updated}), el('dd', {text: D.updated || T.none}),
    D.req.jira.length ? el('dt', {text:T.jira}) : null, D.req.jira.length ? el('dd', {}, [jira]) : null
  ]);
  var warn = isOld ? el('div', {class:'warn'}, [ document.createTextNode(T.viewingOld), el('a', {href:D.latestUrl, text:T.toLatest + ' v' + D.latest}) ]) : null;
  var vs = el('ul', {class:'vs'}, D.versions.map(function(v){
    var cur = v.n === D.current;
    return el('li', {class: cur ? 'cur' : ''}, [ el('a', {href: v.n === D.latest ? D.latestUrl : v.url}, [ el('span', {class:'num', text:'v' + v.n}), el('span', {class:'meta'}, [ document.createTextNode((v.time || '') + (v.by ? ' · ' + v.by : '')), v.note ? el('span', {class:'note', text:v.note}) : null ]), cur ? el('span', {class:'cur-badge', text:T.current}) : (v.n === D.latest ? el('span', {class:'cur-badge', text:T.latest}) : null) ]), v.file ? el('a', {class:'file', href:v.file, title:T.file, text:'\u2193'}) : null ]);
  }));
  var ft = el('div', {class:'ft'}, [ D.doc.dirUrl ? el('a', {class:'btn primary', href:D.doc.dirUrl, text:T.dir}) : null, isOld ? el('a', {class:'btn', href:D.latestUrl, text:T.latest}) : null, D.exportUrl ? el('a', {class:'btn', href:D.exportUrl, text:(D.exportLabel || T.download)}) : null ]);
  var infoSec = el('div', {class:'sec on'}, [warn, dl, el('div', {style:'padding:6px 14px 0;color:#6b7280;font-size:12px', text:T.versions + ' (' + D.versions.length + ')'}), vs, ft]);
  // 留言
  var list = el('ul', {class:'cl'}, [el('li', {class:'empty', text:'…'})]);
  var nameIn = el('input', {type:'text', placeholder:T.yourName, maxlength:'40'});
  var bodyIn = el('textarea', {placeholder:T.yourComment, maxlength:'2000'});
  var msg = el('span', {class:'err'});
  var sendBtn = el('button', {type:'button', text:T.send});
  var form = el('div', {class:'cf'}, [nameIn, bodyIn, el('div', {class:'row'}, [msg, sendBtn])]);
  var commentsSec = el('div', {class:'sec'}, [list, form]);
  try { nameIn.value = localStorage.getItem('dcpm-name') || ''; } catch (e) {}
  var tabInfo = el('button', {type:'button', class:'on', text:T.tabInfo});
  var tabCmt = el('button', {type:'button', text:T.tabComments});
  var tabs = el('div', {class:'tabs'}, [tabInfo, tabCmt]);
  var loaded = false, count = 0;
  function renderList(items){
    while (list.firstChild) list.removeChild(list.firstChild);
    if (!items.length) { list.appendChild(el('li', {class:'empty', text:T.noComments})); return; }
    items.forEach(function(c){ list.appendChild(el('li', {}, [ el('div', {}, [ el('span', {class:'a', text:c.author}), el('span', {class:'t', text:c.time + (c.version ? ' · v' + c.version : '')}), c.resolved ? el('span', {class:'r', text:T.resolved}) : null ]), el('div', {class:'b', text:c.body}) ])); });
  }
  function setCount(n){ count = n; tabCmt.textContent = T.tabComments; if (n) tabCmt.appendChild(el('span', {class:'badge', text:String(n)})); }
  function load(){
    if (!D.commentsUrl) return;
    fetch(D.commentsUrl, {cache:'no-store'}).then(function(r){ return r.json(); }).then(function(items){ loaded = true; renderList(items); setCount(items.length); }).catch(function(){ renderList([]); list.firstChild.textContent = T.loadFail; });
  }
  function show(which){ tabInfo.classList.toggle('on', which === 'info'); tabCmt.classList.toggle('on', which === 'cmt'); infoSec.classList.toggle('on', which === 'info'); commentsSec.classList.toggle('on', which === 'cmt'); if (which === 'cmt' && !loaded) load(); }
  tabInfo.addEventListener('click', function(){ show('info'); }); tabCmt.addEventListener('click', function(){ show('cmt'); });
  sendBtn.addEventListener('click', function(){
    var a = nameIn.value.trim(), b = bodyIn.value.trim();
    msg.className = 'err'; msg.textContent = '';
    if (!a || !b) { msg.textContent = T.needBoth; return; }
    sendBtn.disabled = true; sendBtn.textContent = T.sending;
    var body = new URLSearchParams(); body.append('author', a); body.append('body', b); body.append('version', String(D.current));
    fetch(D.commentsUrl, {method:'POST', body: body}).then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); }).then(function(res){
      sendBtn.disabled = false; sendBtn.textContent = T.send;
      if (!res.ok) { msg.textContent = res.j.error || 'Error'; return; }
      try { localStorage.setItem('dcpm-name', a); } catch (e) {}
      bodyIn.value = ''; msg.className = 'ok'; msg.textContent = 'OK';
      if (list.firstChild && list.firstChild.className === 'empty') list.removeChild(list.firstChild);
      list.insertBefore(el('li', {}, [ el('div', {}, [ el('span', {class:'a', text:res.j.author}), el('span', {class:'t', text:res.j.time + ' · v' + D.current}) ]), el('div', {class:'b', text:res.j.body}) ]), list.firstChild);
      setCount(count + 1);
    }).catch(function(){ sendBtn.disabled = false; sendBtn.textContent = T.send; msg.textContent = 'Error'; });
  });
  var panel = el('div', {class:'panel'}, [hd, tabs, infoSec, commentsSec]);
  load();
  wrap.appendChild(pill); wrap.appendChild(panel);
  var style = document.createElement('style'); style.textContent = css;
  root.appendChild(style); root.appendChild(wrap);
  pill.addEventListener('click', function(){ wrap.classList.add('open'); });
  hd.querySelector('.x').addEventListener('click', function(){ wrap.classList.remove('open'); });
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape') wrap.classList.remove('open'); });
  function mount(){ if (document.body) document.body.appendChild(host); }
  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);
})();
"""


def build_widget(data: dict) -> bytes:
    payload = json.dumps(data, ensure_ascii=True).replace("</", "<\\/")
    js = WIDGET_JS.replace("__DATA__", payload)
    snippet = f"\n<script data-dcpm-widget=\"1\">{js}</script>\n"
    # 脚本内的非 ASCII 只出现在 JS 字符串字面量中，用 \uXXXX 转义（HTML 实体在 <script> 内不会被解码）
    return snippet.encode("ascii", "backslashreplace")


def inject_widget(html: bytes, data: dict) -> bytes:
    snippet = build_widget(data)
    matches = list(_BODY_END.finditer(html))
    if matches:
        m = matches[-1]
        return html[: m.start()] + snippet + html[m.start():]
    return html + snippet
