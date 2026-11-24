/* Librarium 内置 AI 工作台（阶段 2）。
   把出题自测 / 问答 / 学术启发 / 逐字精读 集成为主页右上角小花入口打开的全屏工作台。
   所有交互逻辑自包含；思考状态通过 window.__librariumThinking(on) 通知主页大花动画。 */

(function () {
  "use strict";

  var TEMPLATE =
    '<div class="wrap">' +
      '<div class="wb-head">' +
        '<h1>Librarium · AI 工作台</h1>' +
        '<button class="wb-close" id="wbClose" type="button">关闭</button>' +
      '</div>' +
      '<div class="sub">选择范围 → 模式 → 提问 / 出题自测。引用可点开原文。</div>' +
      '<div class="card">' +
        '<div class="row">' +
          '<label>模式<select id="mode">' +
            '<option value="qa">问答</option>' +
            '<option value="inspire">学术启发</option>' +
            '<option value="quiz">出题自测</option>' +
            '<option value="verbatim">逐字精读</option>' +
          '</select></label>' +
          '<label>外源<select id="external">' +
            '<option value="closed">闭卷（仅书内）</option>' +
            '<option value="semi">半开卷（书内+通识）</option>' +
            '<option value="open">全开卷（+外部）</option>' +
          '</select></label>' +
          '<label class="chk"><input type="checkbox" id="web"> 联网搜索</label>' +
        '</div>' +
        '<div class="scope-panel">' +
          '<button class="scope-toggle" id="scopeToggle" type="button">选择范围 <span class="scope-count" id="scopeCount">已选 0 项</span><span class="arrow">▾</span></button>' +
          '<div class="scope-body" id="scopeBody">' +
            '<input type="text" class="scope-search" id="scopeSearch" placeholder="搜索书名 / 章节 / 标签…">' +
            '<div class="scope-list" id="scopeList"><span class="empty">加载范围中…</span></div>' +
            '<button class="ghost" id="clearScope" type="button" style="margin-top:10px;">清空已选</button>' +
          '</div>' +
        '</div>' +
        '<textarea id="query" placeholder="输入问题，例如：泰利斯为什么提出水是万物的本原？"></textarea>' +
        '<div class="row" style="margin-top:14px; margin-bottom:0;"><button id="send">发送</button></div>' +
        '<div id="status"></div>' +
        '<div id="studyArea">' +
          '<div class="section-head" id="pointsToggle"><span class="fold">▸</span> 知识点清单</div>' +
          '<div class="points-list" id="pointsList"></div>' +
          '<div id="quizCards"></div>' +
          '<div class="progress-wrap">' +
            '<div class="progress-label"><span>知识点覆盖进度</span><span id="progressText">0%（0/0 知识点）</span></div>' +
            '<div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>' +
          '</div>' +
          '<div class="row" style="margin-top:6px;"><button id="moreBtn" class="ghost">再来五题</button></div>' +
          '<div class="section-head" id="favToggle"><span class="fold">▸</span> 收藏列表</div>' +
          '<div class="points-list" id="favList" style="display:none;"></div>' +
        '</div>' +
        '<div id="answer"></div>' +
        '<div id="sources"><h3>引用来源（点击查看原文）</h3><div id="srcList"></div></div>' +
      '</div>' +
    '</div>' +
    '<div class="modal" id="modal"><div class="panel">' +
      '<div class="head"><h3 id="modalTitle">片段原文</h3><button class="ghost" id="modalClose" type="button">关闭</button></div>' +
      '<pre id="modalText"></pre>' +
    '</div></div>';

  var root = document.getElementById("aiWorkbench");
  if (!root) return;
  root.innerHTML = TEMPLATE;

  var $ = function (id) { return root.querySelector("#" + id); };
  var answer = $("answer"), sources = $("sources"), srcList = $("srcList");

  function setThinking(on) {
    if (window.__librariumThinking) window.__librariumThinking(!!on);
  }

  var sourceByMarker = {};
  var ov = null;
  var checked = new Set(), expanded = new Set();
  var booksOpen = true, tagsOpen = true, filter = "";
  var study = { scope: [], points: [], questions: [], covered: new Set(), marked: {}, favorited: {} };

  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function normalizeMarker(s) { var m = String(s || "").match(/S\d+/); return m ? m[0] : ""; }
  function renderText(s) {
    var html = escapeHtml(s || "");
    html = html.replace(/\[S(\d+)\]/g, function (m, n) { return '<span class="cite" data-marker="S' + n + '">' + m + "</span>"; });
    html = html.replace(/\n/g, "<br>");
    return html;
  }
  function renderAnswer(raw) {
    var html = escapeHtml(raw);
    html = html.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    html = html.replace(/\[S(\d+)\]/g, function (m, n) { return '<span class="cite" data-marker="S' + n + '">' + m + "</span>"; });
    html = html.replace(/(第[0-9一二三四五六七八九十百]+章)/g, '<span class="loc">$1</span>');
    html = html.replace(/(片段\d+)/g, '<span class="loc">$1</span>');
    html = html.replace(/\bp\.\d+\b/g, '<span class="loc">$&</span>');
    html = html.replace(/\n/g, "<br>");
    return html;
  }

  function renderScope() {
    var q = filter.trim().toLowerCase(), html = "", booksHtml = "";
    var books = (ov && ov.books) || [];
    for (var bi = 0; bi < books.length; bi++) {
      var b = books[bi], slug = b.slug, bTitle = (b.title || "").toLowerCase(), chapters = b.chapters || [];
      var chMatch = chapters;
      if (q) chMatch = chapters.filter(function (c) { return (c.title || "").toLowerCase().indexOf(q) !== -1 || ("第" + c.index + "章").indexOf(q) !== -1; });
      var bookMatch = !q || bTitle.indexOf(q) !== -1 || slug.toLowerCase().indexOf(q) !== -1;
      if (!bookMatch && !(q && chMatch.length)) continue;
      var showChapters = q ? chMatch : chapters;
      var isOpen = expanded.has(slug) || (q && chMatch.length > 0);
      booksHtml += '<div class="book-item"><div class="book-row">' +
        '<span class="fold" data-fold="' + slug + '">' + (isOpen ? "▾" : "▸") + "</span>" +
        '<label class="scope-item"><input type="checkbox" class="scope-cb" value="book:' + slug + '"' + (checked.has("book:" + slug) ? " checked" : "") + "> 整书</label>" +
        '<span class="book-title" data-fold="' + slug + '">《' + escapeHtml(b.title) + "》</span></div>" +
        '<div class="chapter-list" style="' + (isOpen ? "" : "display:none") + '">' +
        showChapters.map(function (c) {
          return '<label class="scope-item sub"><input type="checkbox" class="scope-cb" value="chapter:' + slug + ":" + c.index + '"' + (checked.has("chapter:" + slug + ":" + c.index) ? " checked" : "") + "> " + escapeHtml(c.title || ("第" + c.index + "章")) + "</label>";
        }).join("") +
        "</div></div>";
    }
    if (booksHtml) html += '<div class="grp grp-fold" data-sec="books"><span class="fold">' + (booksOpen ? "▾" : "▸") + "</span> 书籍</div>" + (booksOpen ? booksHtml : "");
    var tagsHtml = "", tags = (ov && ov.tags) || [];
    for (var ti = 0; ti < tags.length; ti++) {
      var t = tags[ti];
      if (q && t.tag.toLowerCase().indexOf(q) === -1) continue;
      tagsHtml += '<label class="scope-item"><input type="checkbox" class="scope-cb" value="tag:' + t.tag + '"' + (checked.has("tag:" + t.tag) ? " checked" : "") + "> " + escapeHtml(t.tag) + "</label>";
    }
    if (tagsHtml) html += '<div class="grp grp-fold" data-sec="tags"><span class="fold">' + (tagsOpen ? "▾" : "▸") + "</span> 标签</div>" + (tagsOpen ? tagsHtml : "");
    if (!html) html = '<div class="empty">无匹配结果</div>';
    $("scopeList").innerHTML = html;
    updateScopeCount();
  }

  function loadOverview() {
    fetch("/api/corpus/overview").then(function (r) { return r.json(); }).then(function (d) { ov = d; renderScope(); }).catch(function (e) { ov = { books: [], tags: [] }; $("status").textContent = "无法连接后端：" + e; renderScope(); });
  }
  function updateScopeCount() { $("scopeCount").textContent = "已选 " + checked.size + " 项"; }
  function collectScope() {
    var scope = [];
    checked.forEach(function (v) {
      var i = v.indexOf(":"), type = v.slice(0, i), value = v.slice(i + 1);
      if (type === "book") scope.push({ type: "book", value: value });
      else if (type === "chapter") scope.push({ type: "chapter", value: value });
      else if (type === "tag") scope.push({ type: "tag", value: value });
    });
    return scope;
  }

  function showSegment(marker) {
    var src = sourceByMarker[marker];
    var mTitle = $("modalTitle"), mText = $("modalText");
    if (!src) { mTitle.textContent = marker; mText.textContent = "未找到该引用来源。"; $("modal").classList.add("open"); return; }
    mTitle.textContent = src.label + (src.page ? " · p." + src.page : "");
    mText.textContent = "加载原文中…"; $("modal").classList.add("open");
    fetch("/api/corpus/segment/" + encodeURIComponent(src.id)).then(function (r) {
      if (r.ok) return r.json();
      throw new Error("404");
    }).then(function (seg) {
      mTitle.textContent = seg.book_title + " · " + (seg.chapter_title || ("第" + seg.chapter_index + "章")) + " · 片段" + seg.seq + (seg.printed_page || seg.file_page ? " · p." + (seg.printed_page || seg.file_page) : "");
      mText.textContent = seg.text;
    }).catch(function () { mText.textContent = "加载原文失败。"; });
  }

  // ===== 学习会话 =====
  function progressPct() { var t = study.points.length, c = study.covered.size; return t ? Math.round(c / t * 100) : 0; }
  function renderProgress() { $("progressFill").style.width = progressPct() + "%"; $("progressText").textContent = progressPct() + "%（" + study.covered.size + "/" + study.points.length + " 知识点）"; }
  function renderPoints() {
    var html = study.points.map(function (p) { var done = study.covered.has(p.id); return '<span class="point-item ' + (done ? "done" : "") + '"><span class="dot">' + (done ? "●" : "○") + "</span>" + escapeHtml(p.name) + "</span>"; }).join("");
    $("pointsList").innerHTML = html || '<span class="empty">暂无知识点</span>';
  }
  function renderFavorites() {
    var favs = study.questions.filter(function (q) { return study.favorited[q.id]; });
    $("favList").innerHTML = favs.length ? favs.map(function (q) { return '<div class="qcard" style="margin-bottom:8px;"><div class="q">' + renderText(q.question) + '</div><div class="qx">' + renderText(q.answer) + "</div></div>"; }).join("") : '<span class="empty">暂无收藏</span>';
  }
  function renderCards() {
    var html = study.questions.map(function (q) {
      var marker = normalizeMarker(q.source), mk = study.marked[q.id];
      var yesOn = mk === "yes", noOn = mk === "no";
      return '<div class="qcard" data-qid="' + q.id + '">' +
        '<div class="q"><b>' + q.id.replace("q", "") + ".</b> " + renderText(q.question) + "</div>" +
        '<div class="qbtns"><button class="ghost reveal" data-qid="' + q.id + '">查看答案及解析</button>' +
        (marker ? '<button class="ghost srclink" data-marker="' + marker + '">查看原文</button>' : "") +
        (yesOn ? '<span class="qbadge">✓ 已掌握</span>' : "") + (noOn ? '<span class="qbadge">✗ 还不熟</span>' : "") + "</div>" +
        '<div class="qans" style="display:none;">' +
        '<div class="qa"><b>答案</b>：' + renderText(q.answer) + "</div>" +
        '<div class="qx"><b>解析</b>：' + renderText(q.analysis) + "</div>" +
        '<div class="qacts">' +
        '<button class="ghost act-yes ' + (yesOn ? "on" : "") + '" data-qid="' + q.id + '">会</button>' +
        '<button class="ghost act-no ' + (noOn ? "on" : "") + '" data-qid="' + q.id + '">不会</button>' +
        '<button class="ghost act-fav ' + (study.favorited[q.id] ? "on" : "") + '" data-qid="' + q.id + '">' + (study.favorited[q.id] ? "★ 已收藏" : "☆ 收藏") + "</button>" +
        "</div></div></div>";
    }).join("");
    $("quizCards").innerHTML = html || '<div class="empty">还没有题目，点「开始学习」。</div>';
  }

  function postJson(path, payload) {
    return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  }

  function startStudy() {
    var scope = collectScope();
    if (!scope.length) { $("status").textContent = "请先勾选范围。"; return; }
    study = { scope: scope, points: [], questions: [], covered: new Set(), marked: {}, favorited: {} };
    $("studyArea").style.display = "block"; answer.innerHTML = ""; sources.style.display = "none";
    var btn = $("send"); btn.disabled = true; setThinking(true);
    $("status").textContent = "生成知识点清单…";
    postJson("/api/quiz/ledger", { scope: scope }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t); });
      return r.json();
    }).then(function (led) {
      study.points = led.points || [];
      renderProgress(); renderPoints(); renderCards();
      $("status").textContent = "已生成 " + study.points.length + " 个知识点，出题中…";
      return postJson("/api/quiz/generate", { scope: scope, count: 5 });
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t); });
      return r.json();
    }).then(function (gen) {
      sourceByMarker = {}; (gen.sources || []).forEach(function (s) { sourceByMarker[s.marker] = s; });
      if (gen.done) { $("status").textContent = "所有知识点已覆盖！可「再来五题」重做或复习收藏。"; renderCards(); return; }
      study.questions = gen.questions || [];
      renderCards();
      $("status").textContent = "已出 " + study.questions.length + " 题。";
    }).catch(function (e) { $("status").textContent = "开始学习失败：" + e; })
      .finally(function () { btn.disabled = false; setThinking(false); });
  }

  function moreQuestions() {
    if (!study.scope.length) return;
    var btn = $("moreBtn"); btn.disabled = true; setThinking(true);
    $("status").textContent = "出题中…";
    postJson("/api/quiz/generate", { scope: study.scope, count: 5 }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t); });
      return r.json();
    }).then(function (gen) {
      sourceByMarker = {}; (gen.sources || []).forEach(function (s) { sourceByMarker[s.marker] = s; });
      if (gen.done) { $("status").textContent = "所有知识点已覆盖！可重做旧题或复习收藏。"; renderCards(); return; }
      study.questions = study.questions.concat(gen.questions || []);
      renderCards();
      $("status").textContent = "又出 " + (gen.questions || []).length + " 题。";
    }).catch(function (e) { $("status").textContent = "出题失败：" + e; })
      .finally(function () { btn.disabled = false; setThinking(false); });
  }

  function markQuestion(qid, mastered) {
    postJson("/api/quiz/mark", { scope: study.scope, question_id: qid, mastered: mastered }).then(function (r) {
      if (!r.ok) { $("status").textContent = "HTTP " + r.status; return; }
      return r.json();
    }).then(function () {
      study.marked[qid] = mastered ? "yes" : "no";
      var q = study.questions.find(function (x) { return x.id === qid; });
      if (q && q.point_id) { if (mastered) study.covered.add(q.point_id); else study.covered.delete(q.point_id); }
      renderProgress(); renderPoints(); renderCards();
    }).catch(function (e) { $("status").textContent = "标记失败：" + e; });
  }
  function favQuestion(qid) {
    postJson("/api/quiz/favorite", { scope: study.scope, question_id: qid }).then(function (r) {
      if (!r.ok) { $("status").textContent = "HTTP " + r.status; return; }
      return r.json();
    }).then(function (res) {
      study.favorited[qid] = res.favorited;
      renderCards(); renderFavorites();
    }).catch(function (e) { $("status").textContent = "收藏失败：" + e; });
  }

  // ===== 聊天 =====
  function chat() {
    var mode = $("mode").value, external = $("external").value, web = $("web").checked;
    var query = $("query").value.trim();
    if (mode === "verbatim" && !query) query = "请开始逐字精读：先输出本范围的知识点清单。";
    if (!query) { $("status").textContent = "请输入问题（逐字精读可留空直接开始）。"; return; }
    var payload = { query: query, scope: collectScope(), mode: mode, external: external, web: web };
    var btn = $("send"); btn.disabled = true;
    answer.innerHTML = ""; sources.style.display = "none"; srcList.innerHTML = ""; sourceByMarker = {};
    setThinking(true); $("status").textContent = "思考中…";
    fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).then(function (resp) {
      if (!resp.ok) return resp.text().then(function (t) { throw new Error("HTTP " + resp.status + "：" + t); });
      var reader = resp.body.getReader(); var decoder = new TextDecoder(); var buf = "", raw = "";
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) { $("status").textContent = "完成"; answer.innerHTML = renderAnswer(raw); setThinking(false); btn.disabled = false; return; }
          buf += decoder.decode(res.value, { stream: true });
          var i;
          while ((i = buf.indexOf("\n\n")) !== -1) {
            var chunk = buf.slice(0, i); buf = buf.slice(i + 2); var line = chunk.trim();
            if (line.indexOf("data:") !== 0) continue;
            var data; try { data = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
            if (data.type === "meta") { sourceByMarker = {}; (data.sources || []).forEach(function (s) { sourceByMarker[s.marker] = s; }); renderSources(data.sources || []); }
            else if (data.type === "delta") { raw += data.content; answer.innerHTML = renderAnswer(raw) + '<span class="cursor">▍</span>'; }
            else if (data.type === "error") { $("status").textContent = data.message; }
          }
          return pump();
        });
      }
      return pump();
    }).catch(function (e) { $("status").textContent = "请求失败：" + e; setThinking(false); btn.disabled = false; });
  }

  function renderSources(list) {
    if (!list || !list.length) { sources.style.display = "none"; return; }
    sources.style.display = "block";
    srcList.innerHTML = list.map(function (s) { return '<span class="src" data-marker="' + s.marker + '"><b>' + s.marker + "</b> " + escapeHtml(s.label) + "</span>"; }).join("");
  }

  function send() { return $("mode").value === "quiz" ? startStudy() : chat(); }

  function onModeChange() {
    var mode = $("mode").value;
    var label = { qa: "发送", inspire: "发送", quiz: "开始学习", verbatim: "开始精读" }[mode] || "发送";
    $("send").textContent = label;
    $("query").style.display = (mode === "quiz") ? "none" : "";
    $("studyArea").style.display = (mode === "quiz") ? "block" : "none";
    answer.style.display = (mode === "quiz") ? "none" : "";
    $("query").placeholder = (mode === "quiz") ? "" : (mode === "verbatim" ? "（可选）补充要求" : "输入问题，例如：泰利斯为什么提出水是万物的本原？");
  }

  // ===== 事件绑定 =====
  $("send").onclick = send;
  $("query").addEventListener("keydown", function (e) { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send(); });
  $("scopeToggle").onclick = function () { var b = $("scopeBody"); var s = b.style.display === "none"; b.style.display = s ? "block" : "none"; $("scopeToggle").classList.toggle("open", s); };
  $("scopeSearch").addEventListener("input", function (e) { filter = e.target.value; renderScope(); });
  $("clearScope").onclick = function () { checked.clear(); renderScope(); };
  $("scopeList").addEventListener("click", function (e) {
    var sec = e.target.closest(".grp-fold");
    if (sec) { if (sec.getAttribute("data-sec") === "tags") tagsOpen = !tagsOpen; else booksOpen = !booksOpen; renderScope(); return; }
    var fold = e.target.closest("[data-fold]");
    if (fold) { var slug = fold.getAttribute("data-fold"); if (expanded.has(slug)) expanded.delete(slug); else expanded.add(slug); renderScope(); }
  });
  $("scopeList").addEventListener("change", function (e) {
    if (e.target.classList.contains("scope-cb")) { if (e.target.checked) checked.add(e.target.value); else checked.delete(e.target.value); updateScopeCount(); }
  });
  $("answer").addEventListener("click", function (e) {
    var cite = e.target.closest(".cite"); if (cite) { showSegment(cite.getAttribute("data-marker")); }
  });
  $("quizCards").addEventListener("click", function (e) {
    var cite = e.target.closest(".cite"); if (cite) { showSegment(cite.getAttribute("data-marker")); return; }
    var reveal = e.target.closest(".reveal");
    if (reveal) { var i = reveal.getAttribute("data-qid"); var ans = root.querySelector('.qcard[data-qid="' + i + '"] .qans'); if (ans) { var s = ans.style.display === "none"; ans.style.display = s ? "block" : "none"; reveal.textContent = s ? "收起" : "查看答案及解析"; } return; }
    var srclink = e.target.closest(".srclink"); if (srclink) { showSegment(srclink.getAttribute("data-marker")); return; }
    var yes = e.target.closest(".act-yes"); if (yes) { markQuestion(yes.getAttribute("data-qid"), true); return; }
    var no = e.target.closest(".act-no"); if (no) { markQuestion(no.getAttribute("data-qid"), false); return; }
    var fav = e.target.closest(".act-fav"); if (fav) { favQuestion(fav.getAttribute("data-qid")); return; }
  });
  $("srcList").addEventListener("click", function (e) { var el = e.target.closest(".src"); if (el) showSegment(el.getAttribute("data-marker")); });
  $("moreBtn").onclick = moreQuestions;
  $("pointsToggle").onclick = function () { var l = $("pointsList"); var s = l.style.display === "none" || !l.style.display; l.style.display = s ? "flex" : "none"; $("pointsToggle").querySelector(".fold").textContent = s ? "▾" : "▸"; };
  $("favToggle").onclick = function () { var l = $("favList"); var s = l.style.display === "none"; l.style.display = s ? "flex" : "none"; $("favToggle").querySelector(".fold").textContent = s ? "▾" : "▸"; };
  $("modalClose").onclick = function () { $("modal").classList.remove("open"); };
  $("modal").addEventListener("click", function (e) { if (e.target === $("modal")) $("modal").classList.remove("open"); });
  $("mode").addEventListener("change", onModeChange);
  $("wbClose").onclick = function () { closeWorkbench(); };
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && root.classList.contains("open")) closeWorkbench(); });

  function openWorkbench() { root.classList.add("open"); document.body.style.overflow = "hidden"; loadOverview(); }
  function closeWorkbench() { root.classList.remove("open"); document.body.style.overflow = ""; setThinking(false); }

  /* 整朵大花即入口：用文档级事件委托，页面跳转返回后依然有效（不依赖一次性绑定）。 */
  document.addEventListener("click", function (e) {
    if (e.target && e.target.closest && e.target.closest("#artLily")) openWorkbench();
  });

  loadOverview();
  onModeChange();
})();
