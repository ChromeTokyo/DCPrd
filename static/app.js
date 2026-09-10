(function () {
  // 复制链接
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.copy');
    if (!btn) return;
    var text = btn.getAttribute('data-copy');
    var done = function () {
      var old = btn.textContent;
      btn.textContent = '已复制';
      btn.classList.add('copied');
      setTimeout(function () { btn.textContent = old; btn.classList.remove('copied'); }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(text); done(); });
    } else { fallback(text); done(); }
  });
  function fallback(text) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (_) {}
    document.body.removeChild(ta);
  }

  // 页面内确认弹层（不用 window.confirm）
  var modal = document.getElementById('confirm-modal');
  var textEl = document.getElementById('confirm-text');
  var okBtn = document.getElementById('confirm-ok');
  var cancelBtn = document.getElementById('confirm-cancel');
  var pending = null;
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.hasAttribute('data-confirm') || form.dataset.confirmed === '1') return;
    e.preventDefault();
    pending = form;
    textEl.textContent = form.getAttribute('data-confirm');
    modal.hidden = false;
    okBtn.focus();
  });
  function close() { modal.hidden = true; pending = null; }
  cancelBtn.addEventListener('click', close);
  modal.addEventListener('click', function (e) { if (e.target === modal) close(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !modal.hidden) close(); });
  okBtn.addEventListener('click', function () {
    if (!pending) return;
    var f = pending; f.dataset.confirmed = '1'; modal.hidden = true; pending = null;
    f.submit();
  });

  // flash 自动消失
  var flash = document.getElementById('flash');
  if (flash) setTimeout(function () { flash.style.transition = 'opacity .5s'; flash.style.opacity = '0'; }, 4000);

  // 新建需求：复合需求时隐藏首个文件
  var kc = document.getElementById('kind-compound'), ks = document.getElementById('kind-single'), fu = document.getElementById('first-upload');
  if (kc && ks && fu) {
    var sync = function () { fu.hidden = kc.checked; };
    kc.addEventListener('change', sync); ks.addEventListener('change', sync); sync();
  }
})();

// 上传前预览：POST /preview 取渲染结果放进沙箱 iframe
(function () {
  var modal = document.getElementById('preview-modal');
  if (!modal) return;
  var frame = document.getElementById('preview-frame'), title = document.getElementById('preview-title');
  var csrfMeta = document.querySelector('meta[name=csrf]');
  document.querySelectorAll('input[type=file][data-previewable]').forEach(function (input) {
    var btn = input.parentElement.querySelector('.preview-btn');
    if (!btn) return;
    input.addEventListener('change', function () { btn.hidden = !input.files.length; });
    btn.addEventListener('click', function () {
      var f = input.files[0]; if (!f) return;
      var fd = new FormData(); fd.append('file', f); fd.append('csrf', csrfMeta ? csrfMeta.content : '');
      title.textContent = '预览：' + f.name + '（' + Math.round(f.size / 1024) + ' KB）';
      frame.removeAttribute('srcdoc'); frame.srcdoc = '<p style="font:14px sans-serif;padding:20px;color:#666">加载中…</p>';
      modal.hidden = false;
      fetch('/preview', { method: 'POST', body: fd, credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
        .then(function (html) { frame.srcdoc = html; })
        .catch(function (e) { frame.srcdoc = '<p style="font:14px sans-serif;padding:20px;color:#991b1b">预览失败：' + e.message + '</p>'; });
    });
  });
  function close() { modal.hidden = true; frame.srcdoc = ''; }
  document.getElementById('preview-close').addEventListener('click', close);
  modal.addEventListener('click', function (e) { if (e.target === modal) close(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !modal.hidden) close(); });
})();
