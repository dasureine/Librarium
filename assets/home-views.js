/* Librarium 主页 · 视觉小说扉页分级视图脚本
   渲染四项目录菜单，并管理 Taxonomy / Atlases / About 视图与 Scriptorium 入口。
   切换动效仅使用 GPU 合成属性（opacity + transform）。 */
(function () {
  'use strict';

  var MENU = [
    { key: 'taxonomy', label: 'Taxonomy', hint: '概念与学科谱系' },
    { key: 'atlases', label: 'Atlases', hint: '知识图谱书架' },
    { key: 'scriptorium', label: 'Scriptorium', hint: '入藏新书' },
    { key: 'about', label: 'About', hint: '关于我' }
  ];

  var homeMenuEl = document.getElementById('homeMenu');
  var homeViewsEl = document.getElementById('homeViews');
  var contentEl = document.querySelector('.content');

  var booksCache = null;
  var taxonomyCache = null;
  var booksPromise = null;
  var taxonomyPromise = null;

  var activeViewKey = null;
  var activeAtlasFilter = 'all';

  function escapeHtml(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* ---------- 数据缓存 ---------- */
  function ensureBooks() {
    if (!booksPromise) {
      booksPromise = fetch('./books.json', { cache: 'no-store' })
        .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
        .then(function (data) { booksCache = data; return data; })
        .catch(function () { booksCache = booksCache || []; return booksCache; });
    }
    return booksPromise;
  }

  function ensureTaxonomy() {
    if (!taxonomyPromise) {
      taxonomyPromise = fetch('./data/taxonomy.json', { cache: 'no-store' })
        .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
        .then(function (data) { taxonomyCache = data; return data; })
        .catch(function () { taxonomyCache = taxonomyCache || {}; return taxonomyCache; });
    }
    return taxonomyPromise;
  }

  /* ---------- 菜单 ---------- */
  function renderMenu() {
    if (!homeMenuEl) return;
    homeMenuEl.innerHTML = '';
    MENU.forEach(function (m) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'home-menu-item';
      btn.setAttribute('data-view', m.key);
      btn.innerHTML =
        '<span class="hm-diamond" aria-hidden="true"></span>' +
        '<span class="hm-label">' + escapeHtml(m.label) + '</span>' +
        '<span class="hm-hint">' + escapeHtml(m.hint) + '</span>';
      homeMenuEl.appendChild(btn);
    });
  }

  if (homeMenuEl) {
    homeMenuEl.addEventListener('click', function (e) {
      var item = e.target && e.target.closest ? e.target.closest('.home-menu-item') : null;
      if (!item) return;
      var key = item.getAttribute('data-view');
      if (key === 'scriptorium') { openScriptorium(); return; }
      openView(key);
    });
  }

  /* ---------- 视图切换（opacity + transform） ---------- */
  function showView(view) {
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { view.classList.add('is-active'); });
    });
  }

  function openView(key) {
    if (activeViewKey === key && contentEl.classList.contains('view-open')) return;
    dismissView();
    activeViewKey = key;

    var view = document.createElement('div');
    view.className = 'home-view';
    view.setAttribute('data-view', key);
    homeViewsEl.innerHTML = '';
    homeViewsEl.appendChild(view);
    contentEl.classList.add('view-open');
    view.style.display = 'block';

    syncViewParam(key);

    if (key === 'taxonomy') {
      ensureTaxonomy().then(function () {
        view.innerHTML = '';
        view.appendChild(buildTaxonomy());
        showView(view);
      });
    } else if (key === 'atlases') {
      ensureBooks().then(function () {
        view.innerHTML = '';
        view.appendChild(buildAtlases());
        showView(view);
      });
    } else {
      view.appendChild(buildAbout());
      showView(view);
    }
  }

  function syncViewParam(key) {
    try {
      var params = new URLSearchParams(window.location.search);
      params.set('settled', '1');
      if (key === 'taxonomy' || key === 'atlases') {
        params.set('view', key);
      } else {
        params.delete('view');
      }
      var base = window.location.pathname.split('/').pop() || 'index.html';
      window.history.replaceState(null, '', base + '?' + params.toString());
    } catch (e) { /* 无痕降级：同步失败不阻断交互 */ }
  }

  function dismissView() {
    if (!contentEl.classList.contains('view-open')) return;
    contentEl.classList.remove('view-open');
    var view = homeViewsEl.querySelector('.home-view');
    if (view) {
      view.classList.remove('is-active');
      (function (v) {
        setTimeout(function () { if (v.parentNode) v.parentNode.removeChild(v); }, 520);
      })(view);
    }
    activeViewKey = null;
    syncViewParam(null);
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && contentEl.classList.contains('view-open')) dismissView();
  });

  document.addEventListener('click', function (e) {
    if (activeViewKey !== 'about') return;
    if (!contentEl.classList.contains('view-open')) return;
    if (e.target && e.target.closest && e.target.closest('.home-menu-item')) return;
    var frame = homeViewsEl.querySelector('.about-frame');
    if (frame && !frame.contains(e.target)) dismissView();
  });

  /* ---------- 通用头部 ---------- */
  function navCluster() {
    var nav = document.createElement('div');
    nav.className = 'hv-nav';

    var back = document.createElement('button');
    back.type = 'button';
    back.className = 'hv-back';
    back.setAttribute('aria-label', '返回上一级');
    back.innerHTML =
      '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M19 12H5"></path>' +
        '<path d="M10 7L5 12l5 5"></path>' +
      '</svg>';
    back.addEventListener('click', dismissView);

    var sep = document.createElement('span');
    sep.className = 'hv-nav-sep';
    sep.setAttribute('aria-hidden', 'true');

    var brand = document.createElement('button');
    brand.type = 'button';
    brand.className = 'hv-brand';
    brand.setAttribute('aria-label', '返回首页');
    brand.textContent = 'Librarium';
    brand.addEventListener('click', dismissView);

    nav.appendChild(back);
    nav.appendChild(sep);
    nav.appendChild(brand);
    return nav;
  }

  function head(title, extra) {
    var h = document.createElement('div');
    h.className = 'hv-head';
    h.appendChild(navCluster());
    if (title) {
      var t = document.createElement('h2');
      t.className = 'hv-title';
      t.textContent = title;
      h.appendChild(t);
    }
    if (extra) h.appendChild(extra);
    return h;
  }

  /* ---------- Taxonomy 视图 ---------- */
  function buildTaxonomy() {
    var wrap = document.createElement('div');
    wrap.className = 'hv-taxonomy';

    var filterWrap = document.createElement('div');
    filterWrap.className = 'hv-filter';
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'hv-filter-input';
    input.placeholder = '关键词过滤…';
    input.setAttribute('aria-label', '过滤学科标签');
    filterWrap.appendChild(input);
    wrap.appendChild(head('Taxonomy', filterWrap));

    var body = document.createElement('div');
    body.className = 'hv-body taxonomy-body';
    var catalog = document.createElement('div');
    catalog.className = 'taxonomy-catalog';
    var detail = document.createElement('div');
    detail.className = 'taxonomy-detail';
    body.appendChild(catalog);
    body.appendChild(detail);
    wrap.appendChild(body);

    var taxonomy = taxonomyCache || {};
    var primaries = Object.keys(taxonomy);

    primaries.forEach(function (primary) {
      var sec = document.createElement('section');
      sec.className = 'tax-cat';
      var title = document.createElement('h3');
      title.className = 'tax-cat-title';
      title.textContent = primary;
      sec.appendChild(title);
      var tags = document.createElement('div');
      tags.className = 'tax-tags';
      var secondaries = taxonomy[primary] || {};
      Object.keys(secondaries).forEach(function (secondary) {
        var refs = (secondaries[secondary] && secondaries[secondary].references) || [];
        var tag = primary + '/' + secondary;
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'tax-tag';
        btn.setAttribute('data-tag', tag);
        btn.setAttribute('data-count', String(refs.length));
        btn.innerHTML = '<span>' + escapeHtml(secondary) + '</span><span class="tax-count">· ' + refs.length + '</span>';
        btn.addEventListener('click', function () {
          selectTaxonomyTag(primary, secondary, refs, btn);
        });
        tags.appendChild(btn);
      });
      sec.appendChild(tags);
      catalog.appendChild(sec);
    });

    // 默认选中首个二级学科
    var firstPrimary = primaries[0];
    if (firstPrimary) {
      var firstSecondaries = taxonomy[firstPrimary] || {};
      var firstSecondary = Object.keys(firstSecondaries)[0];
      if (firstSecondary) {
        var firstRefs = (firstSecondaries[firstSecondary] && firstSecondaries[firstSecondary].references) || [];
        selectTaxonomyTag(firstPrimary, firstSecondary, firstRefs, catalog.querySelector('.tax-tag'));
      }
    }

    input.addEventListener('input', function () {
      filterTaxonomyTags(catalog, input.value.trim().toLowerCase());
    });

    return wrap;
  }

  function selectTaxonomyTag(primary, secondary, refs, btn) {
    var wrap = btn ? btn.closest('.hv-taxonomy') : document.querySelector('.hv-taxonomy');
    if (!wrap) return;
    var catalog = wrap.querySelector('.taxonomy-catalog');
    var detail = wrap.querySelector('.taxonomy-detail');
    if (catalog) {
      catalog.querySelectorAll('.tax-tag').forEach(function (t) {
        t.classList.toggle('active', t.getAttribute('data-tag') === (primary + '/' + secondary));
      });
    }
    renderTaxonomyDetail(detail, primary, secondary, refs || []);
  }

  function renderTaxonomyDetail(detail, primary, secondary, refs) {
    if (!detail) return;
    detail.innerHTML = '';
    var headEl = document.createElement('div');
    headEl.className = 'tax-detail-head';
    headEl.textContent = primary + ' / ' + secondary + ' · ' + refs.length + ' 条条目';
    detail.appendChild(headEl);
    var list = document.createElement('div');
    list.className = 'tax-ref-list';
    if (!refs.length) {
      list.innerHTML = '<div class="tax-ref-empty">暂无相关书目。</div>';
    } else {
      refs.forEach(function (r) {
        var a = document.createElement('a');
        a.className = 'tax-ref';
        a.href = r.targetUrl || '#';
        a.innerHTML =
          '<div class="tax-ref-meta">' +
            '<span class="tax-ref-book">' + escapeHtml(r.bookTitle) + '</span>' +
            '<span class="tax-ref-scope">' + escapeHtml(r.scope || '整书总图') + '</span>' +
          '</div>' +
          '<span class="tax-ref-node">' + escapeHtml(r.title || r.bookTitle) + '</span>' +
          '<span class="tax-ref-snippet">' + escapeHtml(r.summary || '') + '</span>';
        list.appendChild(a);
      });
    }
    detail.appendChild(list);
  }

  function filterTaxonomyTags(catalog, q) {
    catalog.querySelectorAll('.tax-cat').forEach(function (cat) {
      var visible = 0;
      cat.querySelectorAll('.tax-tag').forEach(function (t) {
        var match = !q || (t.getAttribute('data-tag') || '').toLowerCase().indexOf(q) !== -1;
        t.hidden = !match;
        if (match) visible++;
      });
      cat.hidden = visible === 0;
    });
  }


  /* ---------- Atlases 视图 ---------- */
  function buildAtlases() {
    var wrap = document.createElement('div');
    wrap.className = 'hv-atlases';

    var filterWrap = document.createElement('div');
    filterWrap.className = 'atlases-filter';
    var filters = [
      { key: 'all', label: '全部' },
      { key: 'complete', label: '整书已完结' },
      { key: 'partial', label: '分章更新中' }
    ];
    filters.forEach(function (f) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'atlases-filter-btn' + (f.key === activeAtlasFilter ? ' active' : '');
      b.setAttribute('data-filter', f.key);
      b.textContent = f.label;
      b.addEventListener('click', function () {
        activeAtlasFilter = f.key;
        wrap.querySelectorAll('.atlases-filter-btn').forEach(function (x) {
          x.classList.toggle('active', x === b);
        });
        renderAtlasBooks(wrap.querySelector('.atlases-body'));
      });
      filterWrap.appendChild(b);
    });
    wrap.appendChild(head('Atlases', filterWrap));

    var body = document.createElement('div');
    body.className = 'hv-body atlases-body';
    wrap.appendChild(body);
    renderAtlasBooks(body);
    return wrap;
  }

  function renderAtlasBooks(body) {
    body.innerHTML = '';
    var books = (booksCache || []).filter(function (book) {
      if (activeAtlasFilter === 'all') return true;
      return book.overallStatus === activeAtlasFilter;
    });
    if (!books.length) {
      body.innerHTML = '<div class="hv-panel atlases-empty">当前筛选下暂无书目。</div>';
      return;
    }
    books.forEach(function (book) { body.appendChild(buildAtlasBook(book)); });
  }

  function buildAtlasBook(book) {
    var sec = document.createElement('section');
    sec.className = 'atlas-book';

    var chapters = book.chapters || [];
    var isPartial = book.overallStatus === 'partial' && !(book.url || book.mapPath);

    var card = document.createElement(isPartial ? 'button' : 'a');
    card.className = 'atlas-card';
    if (isPartial) {
      card.type = 'button';
    } else {
      card.href = book.url || book.mapPath || '#';
    }

    var status = isPartial
      ? (chapters.length
          ? '全书 ' + chapters.length + ' 章已上传完整 · 待生成总图'
          : '整书总图待上传完整后生成')
      : (book.overallStatus === 'complete' ? '整书总图' : '已完成 ' + chapters.length + ' 章');
    var tagsText = (book.tags || []).map(escapeHtml).join(' · ');
    card.innerHTML =
      '<div class="atlas-card-top">' +
        '<span class="atlas-badge">' + escapeHtml(status) + '</span>' +
        '<span class="atlas-author">' + escapeHtml(book.author) + '</span>' +
      '</div>' +
      '<h3 class="atlas-title">' + escapeHtml(book.title) + '</h3>' +
      (tagsText ? '<div class="atlas-tags">' + tagsText + '</div>' : '') +
      '<p class="atlas-summary">' + escapeHtml(book.summary) + '</p>' +
      (isPartial ? '' : '<span class="atlas-arrow">→</span>');
    sec.appendChild(card);

    if (isPartial && chapters.length) {
      var action = document.createElement('div');
      action.className = 'atlas-finalize';
      var finalizeBtn = document.createElement('button');
      finalizeBtn.type = 'button';
      finalizeBtn.className = 'atlas-finalize-btn';
      finalizeBtn.textContent = '已完整上传 · 生成整书总图';
      finalizeBtn.title = '分章图谱已齐备，点击生成全书总览与整书知识图谱';
      finalizeBtn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if (finalizeBtn.disabled) return;
        finalizeBtn.disabled = true;
        finalizeBtn.textContent = '正在启动…';
        var starter = (typeof window.startLibrariumFinalize === 'function')
          ? window.startLibrariumFinalize(book.id, book.title, book.author)
          : Promise.resolve(false);
        Promise.resolve(starter).then(function (ok) {
          if (!ok) {
            finalizeBtn.disabled = false;
            finalizeBtn.textContent = '已完整上传 · 生成整书总图';
            showOfflineToast();
          }
        });
      });
      action.appendChild(finalizeBtn);
      sec.appendChild(action);
    }

    var toggleEl = null;
    if (chapters.length) {
      var ch = document.createElement('div');
      ch.className = 'atlas-chapters';
      var toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'atlas-chapters-toggle';
      toggle.setAttribute('aria-expanded', 'true');
      toggle.textContent = '分章 · ' + chapters.length + '　▾';
      toggle.addEventListener('click', function () {
        var collapsed = sec.classList.toggle('chapters-collapsed');
        toggle.setAttribute('aria-expanded', String(!collapsed));
        toggle.textContent = '分章 · ' + chapters.length + (collapsed ? '　▸' : '　▾');
      });
      toggleEl = toggle;
      ch.appendChild(toggle);

      var ul = document.createElement('ul');
      ul.className = 'atlas-chapter-list';
      chapters.forEach(function (c) {
        var li = document.createElement('li');
        var a = document.createElement('a');
        a.className = 'atlas-chapter';
        a.href = c.mapPath || '#';
        a.innerHTML =
          '<span class="atlas-chapter-order">' + String(c.order || 0).padStart(2, '0') + '</span>' +
          '<span class="atlas-chapter-body">' +
            '<span class="atlas-chapter-title">' + escapeHtml(c.title) + '</span>' +
            (c.summary ? '<span class="atlas-chapter-summary">' + escapeHtml(c.summary) + '</span>' : '') +
          '</span>';
        li.appendChild(a);
        ul.appendChild(li);
      });
      ch.appendChild(ul);
      sec.appendChild(ch);
    }

    if (isPartial) {
      card.addEventListener('click', function () {
        var collapsed = sec.classList.toggle('chapters-collapsed');
        if (toggleEl) {
          toggleEl.setAttribute('aria-expanded', String(!collapsed));
          toggleEl.textContent = '分章 · ' + chapters.length + (collapsed ? '　▸' : '　▾');
        }
      });
    }
    return sec;
  }


  /* ---------- About 视图 ---------- */
  function buildAbout() {
    var wrap = document.createElement('div');
    wrap.className = 'hv-about';
    var frame = document.createElement('div');
    frame.className = 'about-frame';
    // TODO: 个人介绍请在此处填写——可在 .about-rule 之后追加 <p> 等内容即可。
    frame.innerHTML =
      '<h2 class="about-title">About</h2>' +
      '<div class="about-rule"></div>';
    wrap.appendChild(frame);
    return wrap;
  }

  /* ---------- Scriptorium 入口 ---------- */
  function openScriptorium() {
    if (typeof window.openLibrariumUploader === 'function') {
      Promise.resolve(window.openLibrariumUploader()).then(function (ok) {
        if (!ok) showOfflineToast();
      });
    } else {
      showOfflineToast();
    }
  }

  var toastTimer = null;
  function showOfflineToast() {
    var toast = document.getElementById('homeToast');
    if (!toast) {
      toast = document.createElement('div');
      toast.id = 'homeToast';
      toast.className = 'home-toast';
      toast.setAttribute('role', 'status');
      document.body.appendChild(toast);
    }
    toast.textContent = '本地工作台未连接，请在本地启动 server.py 后入藏新书';
    toast.classList.add('is-visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.classList.remove('is-visible'); }, 3000);
  }

  /* ---------- 上架完成后刷新缓存与当前视图 ---------- */
  function refreshData() {
    booksPromise = null;
    taxonomyPromise = null;
    var p1 = ensureBooks();
    var p2 = ensureTaxonomy();
    if (activeViewKey === 'atlases') {
      p1.then(function () {
        var v = homeViewsEl.querySelector('.home-view[data-view="atlases"]');
        if (v) { v.innerHTML = ''; v.appendChild(buildAtlases()); }
      });
    } else if (activeViewKey === 'taxonomy') {
      p2.then(function () {
        var v = homeViewsEl.querySelector('.home-view[data-view="taxonomy"]');
        if (v) { v.innerHTML = ''; v.appendChild(buildTaxonomy()); }
      });
    }
  }

  window.addEventListener('librarium:library-updated', refreshData);

  /* ---------- 启动 ---------- */
  function bootFromUrl() {
    var params = new URLSearchParams(window.location.search);
    if (params.get('settled') === '1') {
      document.documentElement.classList.add('settled');
    }
    var view = params.get('view');
    if (view === 'atlases' || view === 'taxonomy') {
      document.documentElement.classList.add('settled');
      openView(view);
    }
  }

  renderMenu();
  ensureBooks();
  ensureTaxonomy();
  bootFromUrl();
})();

