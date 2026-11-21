/* Librarium 入藏控制台（上传 + 自动制图）脚本
   仅依赖本地 FastAPI 后端（/api/*）。线上或纯静态环境探测失败时保持主页纯净。 */
(function () {
  'use strict';

  var API = {
    health: '/api/health',
    upload: '/api/books/upload',
    resplit: '/api/books/resplit',
    start: '/api/books/start',
    status: function (id) { return '/api/books/status/' + encodeURIComponent(id); },
    cancel: function (id) { return '/api/books/cancel/' + encodeURIComponent(id); },
    gitStatus: '/api/git/status',
    gitSync: '/api/git/sync',
    shutdown: '/api/shutdown'
  };

  var STAGES = [
    { key: 'split', label: '切分' },
    { key: 'chapters', label: '逐章笔记与图谱' },
    { key: 'overview', label: '合并笔记' },
    { key: 'map', label: '综合整体地图' },
    { key: 'inject', label: '上架' }
  ];

  var health = null;
  var step = 1;
  var selectedFile = null;
  var chapters = [];
  var needsPageMap = false;
  var pageCount = 0;
  var draft = { slug: '', title: '', author: '' };
  var jobId = null;
  var jobStatus = null;
  var pollTimer = null;
  var paused = false;
  var renderedLogs = 0;
  var activeTag = '全部';

  var btn = null;
  var backdrop = null;
  var modal = null;
  var bodyEl = null;
  var footEl = null;
  var stepsEl = null;
  var closeBtn = null;

  function escapeHtml(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function slugify(str) {
    return String(str == null ? '' : str).trim().toLowerCase()
      .replace(/[^a-z0-9_-]+/g, '-')
      .replace(/^-+|-+$/g, '');
  }

  function fmt(n) {
    return String(n == null ? 0 : n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function humanSize(bytes) {
    if (!bytes && bytes !== 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
  }

  function defaultPageMap() {
    if (!pageCount || pageCount <= 3) return '1-' + (pageCount || 3) + ' | 第一章';
    var third = Math.floor(pageCount / 3);
    return '1-' + third + ' | 第一章\n' +
      (third + 1) + '-' + (2 * third) + ' | 第二章\n' +
      (2 * third + 1) + '-' + pageCount + ' | 第三章';
  }

  function apiFetch(url, opts) {
    return fetch(url, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var msg = (data && (data.detail || data.message)) || ('HTTP ' + res.status);
          throw new Error(msg);
        }
        return data;
      });
    });
  }

  function showError(msg) {
    if (!footEl) return;
    var bar = footEl.querySelector('.lr-error-bar');
    if (!bar) {
      bar = document.createElement('div');
      bar.className = 'lr-error-bar';
      bar.setAttribute('role', 'alert');
      footEl.insertBefore(bar, footEl.firstChild);
    }
    bar.textContent = msg;
  }

  function clearError() {
    if (!footEl) return;
    var bar = footEl.querySelector('.lr-error-bar');
    if (bar) bar.remove();
  }

  /* ---------- 书架刷新（与 index.html 渲染一致） ---------- */

  function positionFilterUnderline(group) {
    var active = group.querySelector('.filter-btn.active');
    var line = group.querySelector('.filter-underline');
    if (active && line) {
      line.style.left = active.offsetLeft + 'px';
      line.style.width = active.offsetWidth + 'px';
    }
  }

  function renderShelf(books, tag) {
    var filterGroupEl = document.getElementById('filterGroup');
    var bookCountEl = document.getElementById('bookCount');
    var bookGridEl = document.getElementById('bookGrid');
    if (!filterGroupEl || !bookCountEl || !bookGridEl) return;
    if (!Array.isArray(books)) books = [];

    var tagSet = new Set(['全部']);
    books.forEach(function (b) {
      if (Array.isArray(b.tags)) b.tags.forEach(function (t) { tagSet.add(t); });
    });
    if (!tagSet.has(tag)) tag = '全部';
    activeTag = tag;

    filterGroupEl.innerHTML = '';
    var line = document.createElement('div');
    line.className = 'filter-underline';
    filterGroupEl.appendChild(line);
    tagSet.forEach(function (t) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'filter-btn' + (t === tag ? ' active' : '');
      b.textContent = t;
      b.addEventListener('click', function () { renderShelf(books, t); });
      filterGroupEl.appendChild(b);
    });

    var filtered = tag === '全部'
      ? books
      : books.filter(function (b) { return Array.isArray(b.tags) && b.tags.indexOf(tag) !== -1; });
    bookCountEl.textContent = String(filtered.length).padStart(2, '0') + ' 卷';
    bookGridEl.innerHTML = '';

    if (filtered.length === 0) {
      bookGridEl.innerHTML = '<div class="status-box">当前分类下暂无书目记录。</div>';
    } else {
      filtered.forEach(function (book, i) {
        var card = document.createElement('a');
        card.className = 'book-card';
        card.href = book.url;
        card.style.animation = 'none';
        card.addEventListener('mousemove', function (e) {
          var r = card.getBoundingClientRect();
          card.style.setProperty('--mx', ((e.clientX - r.left) / r.width * 100) + '%');
          card.style.setProperty('--my', ((e.clientY - r.top) / r.height * 100) + '%');
        });
        var tagsText = (book.tags || []).map(function (t) { return escapeHtml(t); }).join(' · ');
        var dateText = (book.date || '').replace(/-/g, '.');
        card.innerHTML =
          '<div class="card-layout">' +
            '<div class="card-index">' +
              '<span class="card-num">' + String(i + 1).padStart(2, '0') + '</span>' +
              '<span class="card-date">' + escapeHtml(dateText) + '</span>' +
            '</div>' +
            '<div class="card-body">' +
              '<h2 class="card-title">' + escapeHtml(book.title) + '</h2>' +
              (book.subtitle ? '<div class="card-subtitle">' + escapeHtml(book.subtitle) + '</div>' : '') +
              '<div class="card-author">' + escapeHtml(book.author) + '</div>' +
              '<p class="card-summary">' + escapeHtml(book.summary) + '</p>' +
              (tagsText ? '<div class="card-tags">' + tagsText + '</div>' : '') +
            '</div>' +
            '<span class="card-arrow">→</span>' +
          '</div>';
        bookGridEl.appendChild(card);
      });
    }

    requestAnimationFrame(function () { positionFilterUnderline(filterGroupEl); });
  }

  function refreshShelf() {
    fetch('./books.json', { cache: 'no-store' })
      .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
      .then(function (books) { renderShelf(books, activeTag); })
      .catch(function () { /* 拉取失败时保留现有书架，不打断用户 */ });
  }

  /* ---------- 环境探测与统一入口（不再自动插入悬浮按钮） ---------- */

  function detect() {
    fetch(API.health, { method: 'GET', cache: 'no-store' })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        if (data && data.online === true) health = data;
      })
      .catch(function () { /* 线上 / 纯静态环境：后端不在线 */ });
  }

  /* 暴露给主页 Scriptorium 菜单调用：在线则唤起弹窗，返回 Promise<boolean> */
  window.openLibrariumUploader = function () {
    if (health && health.online === true) {
      openModal();
      return Promise.resolve(true);
    }
    return fetch(API.health, { method: 'GET', cache: 'no-store' })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        if (data && data.online === true) {
          health = data;
          openModal();
          return true;
        }
        return false;
      })
      .catch(function () { return false; });
  };

  /* ---------- 弹窗骨架 ---------- */

  function buildModal() {
    if (backdrop) return;
    backdrop = document.createElement('div');
    backdrop.className = 'lr-backdrop';
    backdrop.innerHTML =
      '<section class="lr-modal" role="dialog" aria-modal="true" aria-label="入藏新书">' +
        '<header class="lr-modal-head">' +
          '<div class="lr-modal-title">入藏新书</div>' +
          '<div class="lr-modal-sub">上传本地电子书 · 自动生成笔记与知识图谱</div>' +
          '<button class="lr-close" type="button" aria-label="关闭">&times;</button>' +
        '</header>' +
        '<nav class="lr-steps">' +
          '<span class="lr-step" data-step="1"><i>1</i>选择文件</span>' +
          '<span class="lr-step" data-step="2"><i>2</i>目录预检</span>' +
          '<span class="lr-step" data-step="3"><i>3</i>生成进度</span>' +
        '</nav>' +
        '<div class="lr-body"></div>' +
        '<footer class="lr-foot"></footer>' +
      '</section>';
    document.body.appendChild(backdrop);
    modal = backdrop.querySelector('.lr-modal');
    bodyEl = backdrop.querySelector('.lr-body');
    footEl = backdrop.querySelector('.lr-foot');
    stepsEl = backdrop.querySelector('.lr-steps');
    closeBtn = backdrop.querySelector('.lr-close');

    closeBtn.addEventListener('click', closeModal);
    backdrop.addEventListener('mousedown', function (e) {
      if (e.target === backdrop) closeModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && backdrop.classList.contains('is-open')) closeModal();
    });
  }

  function openModal() {
    console.log('[Librarium] open uploader');
    buildModal();
    if (jobId && jobStatus && (jobStatus === 'queued' || jobStatus === 'running')) {
      renderedLogs = 0;
      goStep(3);
    } else {
      if (step === 3) resetRun();
      goStep(step || 1);
    }
    backdrop.classList.add('is-open');
  }

  function closeModal() {
    if (backdrop) backdrop.classList.remove('is-open');
  }

  function resetRun() {
    step = 1;
    selectedFile = null;
    chapters = [];
    needsPageMap = false;
    pageCount = 0;
    draft = { slug: '', title: '', author: '' };
    jobId = null;
    jobStatus = null;
    paused = false;
    renderedLogs = 0;
    stopPolling();
  }

  function goStep(n) {
    step = n;
    renderSteps();
    if (n === 1) renderStepA();
    else if (n === 2) renderStepB();
    else renderStepC();
  }

  function renderSteps() {
    if (!stepsEl) return;
    var items = stepsEl.querySelectorAll('.lr-step');
    items.forEach(function (el) {
      var s = parseInt(el.getAttribute('data-step'), 10);
      el.classList.toggle('is-active', s === step);
      el.classList.toggle('is-done', s < step);
    });
  }

  function renderStepsDone() {
    if (!stepsEl) return;
    stepsEl.querySelectorAll('.lr-step').forEach(function (el) {
      el.classList.add('is-done');
      el.classList.remove('is-active');
    });
  }

  /* ---------- 步骤 A：选择文件 ---------- */

  function renderStepA() {
    var pricing = (health && health.pricing) || {};
    var peak = pricing.peak;
    var now = pricing.now || (health && health.time) || '';

    var bannerHtml =
      '<div class="lr-banner' + (peak ? ' is-peak' : '') + '">' +
        '<span>北京时间 ' + escapeHtml(now) + '</span>' +
        '<strong>' + escapeHtml(pricing.label || '闲时半价') + '</strong>' +
      '</div>';

    var alertHtml = (health && health.env_configured)
      ? ''
      : '<div class="lr-alert">未检测到有效 <code>API_KEY</code>。请先在项目根目录的 <code>.env</code> 中配置密钥后再开始生成，否则无法调用模型。</div>';

    var hintHtml = selectedFile
      ? ''
      : '<div class="lr-drop-hint" id="lrDropHint">拖拽文件到这里，或<em>点击选择</em></div>';
    var chipHtml = selectedFile
      ? '<div class="lr-file-chip" id="lrFileChip">' +
          '<span class="lr-file-name">' + escapeHtml(selectedFile.name) + '</span>' +
          '<span class="lr-file-size">' + humanSize(selectedFile.size) + '</span>' +
          '<button class="lr-file-clear" type="button" aria-label="移除文件">&times;</button>' +
        '</div>'
      : '<div class="lr-file-chip" id="lrFileChip" hidden></div>';

    bodyEl.innerHTML =
      bannerHtml + alertHtml +
      '<div class="lr-field">' +
        '<label>电子书文件（.pdf / .epub）</label>' +
        '<div class="lr-dropzone" id="lrDrop">' +
          '<input type="file" id="lrFile" accept=".pdf,.epub" hidden>' +
          hintHtml + chipHtml +
        '</div>' +
      '</div>' +
      '<div class="lr-field">' +
        '<label>英文短名（slug）</label>' +
        '<input class="lr-input" id="lrSlug" placeholder="如 xinjiao-lunli" autocomplete="off" value="' + escapeHtml(draft.slug) + '">' +
      '</div>' +
      '<div class="lr-field">' +
        '<label>书名</label>' +
        '<input class="lr-input" id="lrTitle" placeholder="书名" autocomplete="off" value="' + escapeHtml(draft.title) + '">' +
      '</div>' +
      '<div class="lr-field">' +
        '<label>作者</label>' +
        '<input class="lr-input" id="lrAuthor" placeholder="作者" autocomplete="off" value="' + escapeHtml(draft.author) + '">' +
      '</div>';

    footEl.innerHTML =
      '<span class="lr-foot-note">文件仅保存在本地后端 raw_books/</span>' +
      '<button class="lr-btn lr-btn-ghost" type="button" data-act="close">取消</button>' +
      '<button class="lr-btn lr-btn-primary" type="button" id="lrParseBtn">上传并解析目录</button>';

    wireStepA();
  }

  function wireStepA() {
    var drop = bodyEl.querySelector('#lrDrop');
    var fileInput = bodyEl.querySelector('#lrFile');
    var chip = bodyEl.querySelector('#lrFileChip');
    var slugInput = bodyEl.querySelector('#lrSlug');
    var titleInput = bodyEl.querySelector('#lrTitle');
    var authorInput = bodyEl.querySelector('#lrAuthor');
    var parseBtn = footEl.querySelector('#lrParseBtn');
    parseBtn.addEventListener('click', handleUpload);

    function refreshChip() {
      if (selectedFile) {
        var hint = bodyEl.querySelector('#lrDropHint');
        if (hint) hint.remove();
        chip.hidden = false;
        chip.innerHTML =
          '<span class="lr-file-name">' + escapeHtml(selectedFile.name) + '</span>' +
          '<span class="lr-file-size">' + humanSize(selectedFile.size) + '</span>' +
          '<button class="lr-file-clear" type="button" aria-label="移除文件">&times;</button>';
        chip.querySelector('.lr-file-clear').addEventListener('click', function (e) {
          e.stopPropagation();
          selectedFile = null;
          fileInput.value = '';
          refreshChip();
        });
      } else {
        chip.hidden = true;
        chip.innerHTML = '';
        if (!bodyEl.querySelector('#lrDropHint')) {
          drop.insertAdjacentHTML('afterbegin', '<div class="lr-drop-hint" id="lrDropHint">拖拽文件到这里，或<em>点击选择</em></div>');
        }
      }
    }

    function setFile(file) {
      if (!file) return;
      if (!/\.(pdf|epub)$/i.test(file.name)) return;
      selectedFile = file;
      var stem = file.name.replace(/\.[^.]+$/, '');
      var sug = slugify(stem);
      if (sug && !draft.slug) { draft.slug = sug; slugInput.value = sug; }
      if (!draft.title) { draft.title = stem; titleInput.value = stem; }
      refreshChip();
    }

    drop.addEventListener('click', function () { fileInput.click(); });
    fileInput.addEventListener('change', function () { setFile(fileInput.files[0]); });
    ['dragenter', 'dragover'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('is-drag'); });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('is-drag'); });
    });
    drop.addEventListener('drop', function (e) {
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      setFile(f);
    });

    slugInput.addEventListener('input', function () { draft.slug = slugInput.value; });
    titleInput.addEventListener('input', function () { draft.title = titleInput.value; });
    authorInput.addEventListener('input', function () { draft.author = authorInput.value; });

    footEl.querySelector('[data-act="close"]').addEventListener('click', closeModal);
    refreshChip();
  }

  async function handleUpload() {
    console.log('[Librarium] handleUpload: click');
    var parseBtn = footEl.querySelector('#lrParseBtn');
    if (!parseBtn) return;

    try {
      clearError();
      // 点击瞬间锁定按钮并切换文案，防止重复提交
      parseBtn.disabled = true;
      parseBtn.textContent = '正在上传并解析目录…';

      // 前置校验：文件、slug、书名、API Key，任一不通过都给出明确中文提示
      if (!selectedFile) {
        console.warn('[Librarium] handleUpload: 未选择文件');
        showError('请先选择一个 .pdf 或 .epub 电子书文件。');
        return;
      }

      var slug = slugify(draft.slug);
      if (!slug) {
        console.warn('[Librarium] handleUpload: slug 为空');
        showError('请填写有效的英文短名（slug），仅允许字母、数字、下划线与连字符。');
        return;
      }
      draft.slug = slug;
      var slugInput = bodyEl.querySelector('#lrSlug');
      if (slugInput) slugInput.value = slug;

      if (!(draft.title || '').trim()) {
        console.warn('[Librarium] handleUpload: 书名为空');
        showError('请填写书名。');
        return;
      }

      if (health && health.env_configured === false) {
        console.warn('[Librarium] handleUpload: 未配置 API_KEY');
        showError('未检测到有效 API_KEY，请先在项目根目录的 .env 中配置密钥后再上传生成。');
        return;
      }

      var fd = new FormData();
      fd.append('file', selectedFile, selectedFile.name);
      fd.append('slug', slug);
      console.log(
        '[Librarium] handleUpload: POST ' + API.upload +
        ' slug=' + slug +
        ' file=' + selectedFile.name +
        ' size=' + humanSize(selectedFile.size)
      );

      var data = await apiFetch(API.upload, { method: 'POST', body: fd, cache: 'no-store' });
      console.log('[Librarium] handleUpload: 收到响应', data);
      handleSplitResult(data);
    } catch (err) {
      console.error('[Librarium] handleUpload: 异常', err);
      showError('上传失败：' + err.message);
    } finally {
      var btn = footEl.querySelector('#lrParseBtn');
      if (btn) {
        btn.disabled = false;
        btn.textContent = '上传并解析目录';
      }
    }
  }

  function handleSplitResult(data) {
    data = data || {};
    chapters = Array.isArray(data.chapters) ? data.chapters : [];
    needsPageMap = data.status === 'needs_page_map';
    pageCount = data.page_count || 0;

    if (data.status === 'scanned' || data.status === 'empty') {
      showError(data.message || '未能解析出章节');
      return;
    }

    goStep(2);
  }

  /* ---------- 步骤 B：目录预检 ---------- */

  function renderStepB() {
    var content = '';
    if (needsPageMap) {
      content =
        '<div class="lr-field">' +
          '<label>该电子书无内嵌目录，请按「起始页-结束页 | 章节名」填写页码映射（每行一章，页码从 1 起算，结束页含本页）</label>' +
          '<textarea class="lr-input" id="lrPageMap" spellcheck="false">' + escapeHtml(defaultPageMap()) + '</textarea>' +
        '</div>' +
        '<div class="lr-empty">提交后将重新切分，并在下方展示章节与字数。</div>';
    } else if (chapters.length) {
      var rows = chapters.map(function (c) {
        return '<div class="lr-chapter">' +
          '<span class="c-idx">' + String(c.index).padStart(2, '0') + '</span>' +
          '<span class="c-title">' + escapeHtml(c.title) + '</span>' +
          '<span class="c-chars">' + fmt(c.chars) + ' 字</span>' +
          '<span class="c-pages">' + fmt(c.pages) + ' 页</span>' +
        '</div>';
      }).join('');
      content =
        '<div class="lr-chapters">' +
          '<div class="lr-chapters-head"><span>编号</span><span>章节</span><span>字数</span><span>页数</span></div>' +
          '<div class="lr-chapters-list">' + rows + '</div>' +
        '</div>';
    } else {
      content = '<div class="lr-empty">暂无章节信息，请返回上一步重新上传。</div>';
    }

    bodyEl.innerHTML = content +
      '<div class="lr-range">' +
        '<span>运行范围</span>' +
        '<label><input type="radio" name="lrRange" value="full" checked> 运行全书</label>' +
        '<label><input type="radio" name="lrRange" value="first"> 试跑前 <input type="number" id="lrFirstN" value="3" min="1" max="' + Math.max(chapters.length, 1) + '"> 章</label>' +
      '</div>';

    var canStart = chapters.length > 0;
    footEl.innerHTML =
      '<button class="lr-btn lr-btn-ghost" type="button" data-act="back">上一步</button>' +
      (needsPageMap
        ? '<button class="lr-btn lr-btn-primary" type="button" id="lrResplitBtn">重新切分</button>'
        : '<button class="lr-btn lr-btn-primary" type="button" id="lrStartBtn"' + (canStart ? '' : ' disabled') + '>开始生成</button>');

    wireStepB();
  }

  function wireStepB() {
    footEl.querySelector('[data-act="back"]').addEventListener('click', function () { goStep(1); });
    if (needsPageMap) {
      footEl.querySelector('#lrResplitBtn').addEventListener('click', resplit);
    } else {
      var startBtn = footEl.querySelector('#lrStartBtn');
      if (startBtn && !startBtn.disabled) startBtn.addEventListener('click', startJob);
    }
  }

  function resplit() {
    var mapText = bodyEl.querySelector('#lrPageMap').value;
    var btn = footEl.querySelector('#lrResplitBtn');
    btn.disabled = true;
    btn.textContent = '正在重新切分…';

    var fd = new FormData();
    fd.append('slug', slugify(draft.slug) || 'book');
    fd.append('page_map', mapText);

    apiFetch(API.resplit, { method: 'POST', body: fd, cache: 'no-store' })
      .then(function (data) {
        if (data && data.status === 'scanned') {
          btn.disabled = false;
          btn.textContent = '重新切分';
          var empty = bodyEl.querySelector('.lr-empty');
          if (empty) empty.textContent = data.message || '切分失败';
          return;
        }
        chapters = Array.isArray(data.chapters) ? data.chapters : [];
        needsPageMap = data.status === 'needs_page_map';
        pageCount = data.page_count || 0;
        renderStepB();
      })
      .catch(function (err) {
        btn.disabled = false;
        btn.textContent = '重新切分';
        var empty = bodyEl.querySelector('.lr-empty');
        if (empty) empty.textContent = '切分失败：' + err.message;
      });
  }

  function startJob() {
    var range = 'full';
    var rangeEl = bodyEl.querySelector('input[name="lrRange"]:checked');
    if (rangeEl) range = rangeEl.value;
    var chapterRange = null;
    if (range === 'first') {
      var n = parseInt(bodyEl.querySelector('#lrFirstN').value, 10);
      if (!n || n < 1) n = 1;
      chapterRange = '1-' + n;
    }
    var slug = slugify(draft.slug) || 'book';
    draft.slug = slug;

    var btn = footEl.querySelector('#lrStartBtn');
    btn.disabled = true;
    btn.textContent = '正在启动…';

    var fd = new FormData();
    fd.append('title', draft.title || '未命名书');
    fd.append('author', draft.author || '佚名');
    fd.append('slug', slug);
    if (chapterRange) fd.append('chapter_range', chapterRange);

    apiFetch(API.start, { method: 'POST', body: fd, cache: 'no-store' })
      .then(function (data) {
        jobId = data.job_id;
        jobStatus = data.status || 'queued';
        goStep(3);
        startPolling();
      })
      .catch(function (err) {
        btn.disabled = false;
        btn.textContent = '开始生成';
        var rangeWrap = bodyEl.querySelector('.lr-range');
        if (rangeWrap) {
          rangeWrap.insertAdjacentHTML('beforebegin', '<div class="lr-alert">启动失败：' + escapeHtml(err.message) + '</div>');
        }
      });
  }

  /* ---------- 步骤 C：实时进度 ---------- */

  function renderStepC() {
    var stagesHtml = STAGES.map(function (s) {
      return '<div class="lr-stage" data-stage="' + s.key + '">' + escapeHtml(s.label) + '</div>';
    }).join('');

    bodyEl.innerHTML =
      '<div class="lr-stagebar" id="lrStagebar">' + stagesHtml + '</div>' +
      '<div class="lr-progress"><div class="lr-progress-bar" id="lrProgressBar"></div></div>' +
      '<div class="lr-progress-label">' +
        '<span id="lrProgressText">准备中…</span>' +
        '<span id="lrChapterCount"></span>' +
      '</div>' +
      '<div class="lr-tokens" id="lrTokens">已用 tokens：<b>0</b></div>' +
      '<div class="lr-log" id="lrLog"></div>';

    footEl.innerHTML =
      '<span class="lr-foot-note">暂停仅冻结界面刷新，后端仍会继续生成</span>' +
      '<button class="lr-btn lr-btn-ghost" type="button" id="lrPauseBtn">暂停刷新</button>' +
      '<button class="lr-btn lr-btn-danger" type="button" id="lrCancelBtn">中止生成</button>';

    wireStepC();
  }

  function wireStepC() {
    footEl.querySelector('#lrPauseBtn').addEventListener('click', togglePause);
    footEl.querySelector('#lrCancelBtn').addEventListener('click', cancelJob);
  }

  /* ---------- 轮询与实时进度 ---------- */

  function startPolling() {
    stopPolling();
    paused = false;
    renderedLogs = 0;
    schedulePoll(0);
  }

  function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function schedulePoll(delay) {
    stopPolling();
    pollTimer = setTimeout(function () {
      if (paused) { schedulePoll(700); return; }
      tick();
    }, delay || 0);
  }

  function tick() {
    if (!jobId) return;
    apiFetch(API.status(jobId), { cache: 'no-store' })
      .then(applyStatus)
      .catch(function (err) {
        appendLog('状态轮询失败：' + err.message);
        schedulePoll(2000);
      });
  }

  function isTerminal(s) {
    return s === 'done' || s === 'error' || s === 'cancelled';
  }

  function applyStatus(data) {
    if (!data) { schedulePoll(1500); return; }
    jobStatus = data.status;
    renderStatus(data);
    if (isTerminal(jobStatus)) {
      stopPolling();
      finalize(data);
    } else {
      schedulePoll(1500);
    }
  }

  function renderStatus(data) {
    var stage = data.stage || 'pending';
    var stageIdx = -1;
    for (var i = 0; i < STAGES.length; i++) {
      if (STAGES[i].key === stage) stageIdx = i;
    }

    var stageEls = bodyEl.querySelectorAll('#lrStagebar .lr-stage');
    stageEls.forEach(function (el, idx) {
      el.classList.toggle('is-done', stageIdx > idx || data.status === 'done');
      el.classList.toggle('is-active', stageIdx === idx && data.status === 'running');
    });

    var bar = bodyEl.querySelector('#lrProgressBar');
    var txt = bodyEl.querySelector('#lrProgressText');
    var cnt = bodyEl.querySelector('#lrChapterCount');

    if (data.status === 'done') {
      bar.style.width = '100%';
      txt.textContent = '已完成';
      cnt.textContent = '';
    } else if (stage === 'chapters' && data.total_chapters) {
      var pct = Math.round((data.done_chapters / data.total_chapters) * 100);
      bar.style.width = pct + '%';
      txt.textContent = '逐章生成中';
      cnt.textContent = data.done_chapters + ' / ' + data.total_chapters + ' 章';
    } else if (stageIdx >= 0) {
      bar.style.width = ((stageIdx / STAGES.length) * 100) + '%';
      txt.textContent = STAGES[stageIdx].label;
      cnt.textContent = '';
    } else {
      bar.style.width = '0%';
      txt.textContent = '排队中…';
      cnt.textContent = '';
    }

    var tokens = data.tokens || {};
    bodyEl.querySelector('#lrTokens').innerHTML =
      '已用 tokens：<b>' + fmt(tokens.total) + '</b>' +
      '<span>输入 ' + fmt(tokens.prompt) + '</span>' +
      '<span>输出 ' + fmt(tokens.completion) + '</span>';

    appendLogs(data.logs);
  }

  function appendLogs(logs) {
    if (!Array.isArray(logs)) return;
    var logEl = bodyEl.querySelector('#lrLog');
    if (!logEl) return;
    var newLines = logs.slice(renderedLogs);
    if (newLines.length) {
      renderedLogs += newLines.length;
      logEl.textContent += (logEl.textContent ? '\n' : '') + newLines.join('\n');
      if (!paused) logEl.scrollTop = logEl.scrollHeight;
    }
  }

  function appendLog(line) {
    var logEl = bodyEl.querySelector('#lrLog');
    if (!logEl) return;
    logEl.textContent += (logEl.textContent ? '\n' : '') + line;
    if (!paused) logEl.scrollTop = logEl.scrollHeight;
  }

  function togglePause() {
    paused = !paused;
    var btn = footEl.querySelector('#lrPauseBtn');
    if (btn) btn.textContent = paused ? '继续刷新' : '暂停刷新';
    if (!paused) schedulePoll(0);
  }

  function cancelJob() {
    if (!jobId) return;
    var btn = footEl.querySelector('#lrCancelBtn');
    if (btn) { btn.disabled = true; btn.textContent = '正在中止…'; }
    apiFetch(API.cancel(jobId), { method: 'POST', cache: 'no-store' })
      .then(function () {
        appendLog('已请求中止（当前章节完成后中止，已写章节保留）');
      })
      .catch(function (err) {
        if (btn) { btn.disabled = false; btn.textContent = '中止生成'; }
        appendLog('中止请求失败：' + err.message);
      });
  }

  function finalize(data) {
    /* 新书上架完成后，通知主页刷新 Atlases 与 Taxonomy 缓存数据 */
    window.dispatchEvent(new CustomEvent('librarium:library-updated'));

    if (data.status === 'done' && data.result && data.result.map_url) {
      bodyEl.innerHTML =
        '<div class="lr-success">' +
          '<div class="lr-done-mark">✓</div>' +
          '<h3>已生成并上架</h3>' +
          '<p>' + escapeHtml(data.title || '') + ' 的知识地图已写入书架。</p>' +
        '</div>';
      footEl.innerHTML =
        '<button class="lr-btn lr-btn-ghost" type="button" data-act="close">关闭</button>' +
        '<button class="lr-btn lr-btn-ghost" type="button" data-act="git">同步至 GitHub</button>' +
        '<a class="lr-btn lr-btn-primary" href="' + escapeHtml(data.result.map_url) + '">查看新书地图</a>';
      footEl.querySelector('[data-act="close"]').addEventListener('click', closeModal);
      footEl.querySelector('[data-act="git"]').addEventListener('click', syncToGit);
      renderStepsDone();
    } else if (data.status === 'cancelled') {
      bodyEl.innerHTML =
        '<div class="lr-empty">任务已中止，已完成的章节保留在硬盘，可断点续跑。</div>';
      footEl.innerHTML = '<button class="lr-btn lr-btn-ghost" type="button" data-act="close">关闭</button>';
      footEl.querySelector('[data-act="close"]').addEventListener('click', closeModal);
    } else {
      bodyEl.innerHTML = '<div class="lr-alert">生成失败：' + escapeHtml(data.error || '未知错误') + '</div>';
      footEl.innerHTML =
        '<button class="lr-btn lr-btn-ghost" type="button" data-act="close">关闭</button>' +
        '<button class="lr-btn lr-btn-ghost" type="button" data-act="back">返回重试</button>';
      footEl.querySelector('[data-act="close"]').addEventListener('click', closeModal);
      footEl.querySelector('[data-act="back"]').addEventListener('click', function () { goStep(2); });
    }
  }

  /* ---------- 同步 / 休眠控制（GitHub 一键同步 + 本地服务休眠） ---------- */

  function getFoot() {
    return footEl || document.querySelector('.lr-foot');
  }

  function showSyncNotice(msg) {
    var foot = getFoot();
    if (!foot) return;
    var bar = foot.querySelector('.lr-notice-bar');
    if (!bar) {
      bar = document.createElement('div');
      bar.className = 'lr-notice-bar';
      foot.insertBefore(bar, foot.firstChild);
    }
    bar.textContent = msg;
  }

  function refreshGitStatus() {
    apiFetch(API.gitStatus, { cache: 'no-store' })
      .then(function (data) {
        var foot = getFoot();
        if (!foot) return;
        var dotEl = foot.querySelector('#lrGitDot');
        if (dotEl) dotEl.hidden = !(data && data.has_changes);
      })
      .catch(function () { /* 后端未就绪 / 无 git 时静默忽略 */ });
  }

  function syncToGit() {
    var foot = getFoot();
    if (!foot) return;
    var btn = foot.querySelector('#lrGitBtn') || foot.querySelector('[data-act="git"]');
    if (!btn || btn.disabled) return;
    var label = btn.querySelector('.lr-tool-label');
    btn.disabled = true;
    if (label) label.textContent = '正在推送…';
    else btn.textContent = '正在推送…';
    apiFetch(API.gitSync, { method: 'POST', cache: 'no-store' })
      .then(function (data) {
        showSyncNotice((data && data.message) || '已同步至 GitHub');
      })
      .catch(function (err) {
        showSyncNotice('同步失败：' + err.message);
      })
      .then(function () {
        if (label) label.textContent = '同步至 GitHub';
        else btn.textContent = '同步至 GitHub';
        btn.disabled = false;
        refreshGitStatus();
      });
  }

  function sleepService() {
    var foot = getFoot();
    var btn = foot && foot.querySelector('#lrSleepBtn');
    if (btn) btn.disabled = true;
    apiFetch(API.shutdown, { method: 'POST', cache: 'no-store' })
      .then(function () {
        showSyncNotice('本地服务已休眠，下次双击桌面图标即可唤醒');
      })
      .catch(function (err) {
        showSyncNotice('休眠失败：' + err.message);
        if (btn) btn.disabled = false;
      });
  }

  function mountSideTools(foot) {
    if (!foot) return;
    var host = foot.querySelector('.lr-foot-note');
    if (!host) return; // 完成 / 取消等界面自带按钮区，不注入左侧工具
    if (host.querySelector('.lr-side-tools')) return;
    var bar = document.createElement('span');
    bar.className = 'lr-side-tools';
    bar.innerHTML =
      '<button class="lr-tool" type="button" id="lrGitBtn">' +
        '<span class="lr-tool-label">同步至 GitHub</span>' +
        '<span class="lr-git-dot" id="lrGitDot" hidden></span>' +
      '</button>' +
      '<button class="lr-tool" type="button" id="lrSleepBtn">休眠服务</button>';
    host.appendChild(bar);
    bar.querySelector('#lrGitBtn').addEventListener('click', syncToGit);
    bar.querySelector('#lrSleepBtn').addEventListener('click', sleepService);
    refreshGitStatus();
  }

  // 每个步骤重建 footer 后，自动把「同步 / 休眠」工具挂回左侧状态区
  (function () {
    var target = document.body || document.documentElement;
    if (!target) return;
    new MutationObserver(function () {
      var foot = getFoot();
      if (foot) mountSideTools(foot);
    }).observe(target, { childList: true, subtree: true });
  })();

  /* ---------- 启动 ---------- */
  detect();
})();







