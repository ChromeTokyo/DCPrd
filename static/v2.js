(function () {
  // ---- 菜单树过滤 ----
  var filter = document.getElementById('tree-filter');
  if (filter) {
    filter.addEventListener('input', function () {
      var q = filter.value.trim().toLowerCase();
      var scroll = document.querySelector('.tree-scroll');
      scroll.querySelectorAll('li').forEach(function (li) { li.classList.remove('hide'); });
      if (!q) return;
      scroll.querySelectorAll('li').forEach(function (li) {
        var own = (li.getAttribute('data-title') || '');
        var anyChild = Array.prototype.some.call(li.querySelectorAll('li'), function (c) { return (c.getAttribute('data-title') || '').indexOf(q) >= 0; });
        if (own.indexOf(q) < 0 && !anyChild) li.classList.add('hide');
        else { var d = li.querySelector('details'); if (d) d.open = true; }
      });
      scroll.querySelectorAll('details').forEach(function (d) { if (q) d.open = true; });
    });
  }

  var viewer = document.getElementById('viewer');
  if (!viewer) return;
  var versionId = viewer.getAttribute('data-version');
  var csrf = viewer.getAttribute('data-csrf');
  var media = viewer.getAttribute('data-media');
  var canAnnotate = viewer.getAttribute('data-can-annotate') === '1';
  var frameNew = document.getElementById('frame-new');
  var frameBase = document.getElementById('frame-base');
  var layer = document.getElementById('ann-layer');
  var stages = document.getElementById('stages');

  // ---- iframe 高度自适应（子页面通过 postMessage 上报） ----
  window.addEventListener('message', function (ev) {
    var d = ev.data || {};
    if (d.type === 'dcpm-size' && d.height) {
      [frameNew, frameBase].forEach(function (f) {
        if (f && f.contentWindow === ev.source) {
          var target = Math.min(Math.max(d.height + 8, 300), 20000);
          var cur = parseInt(f.style.height || '0', 10);
          if (Math.abs(target - cur) > 16) f.style.height = target + 'px';
        }
      });
    }
    if (d.type === 'dcpm-ready' && frameNew && frameNew.contentWindow === ev.source) {
      var hint = document.getElementById('diff-hint');
      if (d.total && d.found < d.total && hint) hint.textContent = '有 ' + (d.total - d.found) + ' 处改动未能在页面中定位';
    }
  });

  // ---- 圈出改动开关 ----
  var toggleDiff = document.getElementById('toggle-diff');
  if (toggleDiff) toggleDiff.addEventListener('change', function () {
    if (frameNew) frameNew.contentWindow.postMessage({ type: 'dcpm-toggle', on: toggleDiff.checked }, '*');
    if (layer) layer.classList.toggle('hide-auto', !toggleDiff.checked);
  });

  // ---- 并排对比 ----
  var toggleSplit = document.getElementById('toggle-split');
  var baseStage = document.querySelector('.stage.base');
  if (toggleSplit && baseStage) toggleSplit.addEventListener('change', function () {
    baseStage.hidden = !toggleSplit.checked;
    stages.classList.toggle('split', toggleSplit.checked);
    if (toggleSplit.checked && frameBase && !frameBase.src) frameBase.src = frameBase.getAttribute('data-src');
  });

  // ---- 改动清单点击定位 ----
  var list = document.getElementById('change-list');
  if (list) list.addEventListener('click', function (e) {
    var li = e.target.closest('li[data-index]');
    if (!li) return;
    list.querySelectorAll('li').forEach(function (x) { x.classList.remove('active'); });
    li.classList.add('active');
    var idx = parseInt(li.getAttribute('data-index'), 10);
    if (media === 'html' && frameNew) {
      frameNew.contentWindow.postMessage({ type: 'dcpm-focus', index: idx }, '*');
      // 若 iframe 已被拉高到内容高度，外层需要滚动到大致位置：交给子页面 scrollIntoView 后，父页面滚到 iframe 顶部附近即可
      var r = frameNew.getBoundingClientRect();
      if (r.top < 0 || r.top > window.innerHeight) frameNew.scrollIntoView({ block: 'start' });
    } else if (layer) {
      var box = layer.querySelector('.box.auto[data-index="' + idx + '"]');
      if (box) { box.scrollIntoView({ block: 'center', behavior: 'smooth' }); box.classList.add('hit'); setTimeout(function () { box.classList.remove('hit'); }, 1500); }
    }
  });

  // ---- 手动圈选 ----
  var annBtn = document.getElementById('toggle-annotate');
  if (canAnnotate && annBtn && layer) {
    var drawing = false, start = null, draft = null;
    annBtn.addEventListener('click', function () {
      drawing = !drawing;
      layer.classList.toggle('drawing', drawing);
      annBtn.textContent = drawing ? '完成圈选' : '＋ 手动圈选';
      annBtn.classList.toggle('btn-primary', drawing);
    });
    function rel(ev) {
      var r = layer.getBoundingClientRect();
      return { x: Math.min(Math.max((ev.clientX - r.left) / r.width, 0), 1), y: Math.min(Math.max((ev.clientY - r.top) / r.height, 0), 1) };
    }
    layer.addEventListener('mousedown', function (ev) {
      if (!drawing || ev.target.closest('.box')) return;
      ev.preventDefault();
      start = rel(ev);
      draft = document.createElement('div'); draft.className = 'box draft'; layer.appendChild(draft);
    });
    layer.addEventListener('mousemove', function (ev) {
      if (!drawing || !start || !draft) return;
      var p = rel(ev);
      var x = Math.min(start.x, p.x), y = Math.min(start.y, p.y), w = Math.abs(p.x - start.x), h = Math.abs(p.y - start.y);
      draft.style.left = x * 100 + '%'; draft.style.top = y * 100 + '%'; draft.style.width = w * 100 + '%'; draft.style.height = h * 100 + '%';
      draft._rect = { x: x, y: y, w: w, h: h };
    });
    function finish() {
      if (!start || !draft) return;
      var rect = draft._rect; var el = draft; start = null; draft = null;
      if (!rect || rect.w < 0.005 || rect.h < 0.005) { el.remove(); return; }
      var text = window.prompt('这处改动的说明：', '');
      if (text === null) { el.remove(); return; }
      fetch('/v2/annotations/' + versionId, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF': csrf }, body: JSON.stringify({ x: rect.x, y: rect.y, w: rect.w, h: rect.h, text: text }) })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function (data) {
          el.className = 'box manual'; el.setAttribute('data-id', data.id); el.title = text + ' — ' + data.author;
          var ul = document.getElementById('ann-list');
          var n = ul.querySelectorAll('li').length + 1;
          el.innerHTML = '<span class="num">M' + n + '</span>';
          var li = document.createElement('li'); li.setAttribute('data-id', data.id);
          li.innerHTML = '<span class="num">M' + n + '</span> ' + escapeHtml(text || '（无说明）') + ' <span class="muted small">' + escapeHtml(data.author) + '</span> <button type="button" class="btn-x ann-del" title="删除">×</button>';
          ul.appendChild(li);
          var cnt = document.getElementById('ann-count'); if (cnt) cnt.textContent = '(' + n + ')';
        })
        .catch(function (e) { el.remove(); alert('保存失败：' + e.message); });
    }
    layer.addEventListener('mouseup', finish);
    layer.addEventListener('mouseleave', function () { if (start) finish(); });
    document.addEventListener('click', function (e) {
      var btn = e.target.closest('.ann-del');
      if (!btn) return;
      var li = btn.closest('li'); var id = li.getAttribute('data-id');
      fetch('/v2/annotations/' + versionId + '/' + id + '/delete', { method: 'POST', headers: { 'X-CSRF': csrf } })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); li.remove(); var b = layer.querySelector('.box.manual[data-id="' + id + '"]'); if (b) b.remove(); })
        .catch(function (e) { alert('删除失败：' + e.message); });
    });
  }
  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
})();
