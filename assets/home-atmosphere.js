/* Librarium 主页 · 动态氛围脚本
   底纹大花惯性变速旋转 + 全屏斜落雨丝。
   雨丝（含远景与主景、100% 命中）碰撞中央旋转大花、右上角几何百合与主页文字后弹开。
   仅叠加动效，不改动入场动画、四模块切换与 ?settled=1 机制。
   核心可调常量（雨丝密度/速度/明度线宽/飞溅物理）集中在下方 CFG 对象中。 */
(function () {
  'use strict';

  var svg = document.getElementById('introSvg');
  var stage = document.getElementById('introStage');
  var contentEl = document.querySelector('.content');
  if (!svg || !stage) return;

  /* 定位底纹大花内部包裹图形 <g>（introSvg 的第二个 <g>，即 gFlower）。
     优先旋转该 <g>，避免与入场动画自带的 scale/translate 冲突。 */
  var flower = null;
  var gs = [];
  for (var i = 0; i < svg.children.length; i++) {
    var node = svg.children[i];
    if (node.nodeName && node.nodeName.toLowerCase() === 'g') gs.push(node);
  }
  flower = gs[1] || null;

  /* ---- 视口尺寸与花心/有效半径（与 introSvg 的投影中心保持一致） ---- */
  var W = 0, H = 0, CX = 0, CY = 0, SCALE = 1, R = 160;
  var LEAF_R = 250; /* 与 index.html 入场引擎的花瓣长度一致 */
  function measure() {
    W = window.innerWidth || document.documentElement.clientWidth || 0;
    H = window.innerHeight || document.documentElement.clientHeight || 0;
    CX = W * 0.5;
    CY = H * (W < 640 ? 0.40 : 0.44);
    SCALE = Math.min(1, W / 920, H / 760);
    if (SCALE < 0.46) SCALE = 0.46;
    if (SCALE > 1.15) SCALE = 1.15;
    R = LEAF_R * SCALE; /* 花瓣尖端近似外径 */
  }
  measure();

  /* ---- 状态 ---- */
  var hoverActive = false;
  var viewOpen = false;
  var modalOpen = false;

  /* ---- 旋转 ---- */
  var angle = 0;
  var omega = 0;
  var DEG_IDLE = 7;    /* 静止慢转 ≈51s/圈 */
  var DEG_HOVER = 85;  /* 悬停高速，与慢转拉开明显物理反差 */

  /* ---- 光晕与降暗 ---- */
  var glow = 0;   /* 0..1 */
  var dim = 0;    /* 0..1 */
  var lastFilter = '';

  function isSettled() {
    /* 仅以 intro-stage 为准：home-views.js 在 ?view= 直达时会给 <html>
       加 settled，但此时翻书动画仍在播放，不应提前旋转大花。 */
    return stage.classList.contains('settled');
  }

  function applyRotation() {
    if (!flower) return;
    flower.setAttribute('transform', 'rotate(' + angle.toFixed(2) + ' ' + CX.toFixed(1) + ' ' + CY.toFixed(1) + ')');
  }
  function clearRotation() {
    if (flower && flower.hasAttribute('transform')) flower.removeAttribute('transform');
    angle = 0;
    omega = 0;
  }

  function applyGlow() {
    var b = 1 + glow * 0.45 - dim * 0.4;
    var blur = glow * 16;
    var gAlpha = glow * 0.22;
    var parts = [];
    if (Math.abs(b - 1) > 0.003) parts.push('brightness(' + b.toFixed(3) + ')');
    if (blur > 0.4) parts.push('drop-shadow(0 0 ' + blur.toFixed(1) + 'px rgba(235,232,224,' + gAlpha.toFixed(3) + '))');
    var filter = parts.join(' ');
    if (filter !== lastFilter) {
      stage.style.filter = filter;
      lastFilter = filter;
    }
  }
  function clearGlow() {
    if (stage.style.filter) stage.style.filter = '';
    lastFilter = '';
    glow = 0;
    dim = 0;
  }

  /* ---- 悬停追踪（事件委托，兼容菜单由 home-views.js 渲染） ---- */
  function isHoverTarget(el) {
    return !!(el && el.closest && (el.closest('.brand') || el.closest('.home-menu-item')));
  }
  document.addEventListener('mouseover', function (e) {
    if (isHoverTarget(e.target)) hoverActive = true;
  });
  document.addEventListener('mouseout', function (e) {
    if (!isHoverTarget(e.target)) return;
    if (!isHoverTarget(e.relatedTarget)) hoverActive = false;
  });

  /* ---- 全屏雨丝 Canvas（自动适配 devicePixelRatio） ---- */
  var DPR = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  var rainCanvas = document.createElement('canvas');
  rainCanvas.className = 'home-atmo-canvas home-atmo-rain';
  rainCanvas.setAttribute('aria-hidden', 'true');
  document.body.appendChild(rainCanvas);
  var rainCtx = rainCanvas.getContext('2d');

  function resizeCanvas() {
    DPR = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    rainCanvas.width = Math.round(W * DPR);
    rainCanvas.height = Math.round(H * DPR);
    rainCanvas.style.width = W + 'px';
    rainCanvas.style.height = H + 'px';
    rainCtx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }
  resizeCanvas();
  /* ---- 左下角卢克莱修《物性论》题词（方案甲排版，与入场翻书协同） ---- */
  var epigraphEl = document.createElement('figure');
  epigraphEl.className = 'home-epigraph';
  epigraphEl.innerHTML =
    '<p><span class="ep-drop">Q</span>uod nisi declinare solerent, omnia deorsum,<br>' +
    'imbris uti guttae, caderent per inane profundum\u2026</p>' +
    '<p class="ep-sign">\u2014 Lucretius, II.221\u2013222</p>';
  document.body.appendChild(epigraphEl);

  var textBoxes = [];
  var lilyBoxes = [];
  var epigraphVisible = false;
  var textCollisionActive = false;

  function collectTextRects(root, out) {
    if (!root || !out) return;
    var walker = document.createTreeWalker(root, 4, null); /* 4 = NodeFilter.SHOW_TEXT */
    var node;
    while ((node = walker.nextNode())) {
      var txt = node.nodeValue;
      if (!txt || !txt.replace(/\s/g, '').length) continue;
      var range = document.createRange();
      range.selectNodeContents(node);
      var rects = range.getClientRects();
      for (var i = 0; i < rects.length; i++) {
        var r = rects[i];
        if (r.width > 0 && r.height > 0) out.push({ x: r.left, y: r.top, w: r.width, h: r.height });
      }
    }
  }

  function cacheTextBoxes() {
    textBoxes.length = 0;
    if (!textCollisionActive) return;
    if (epigraphEl && epigraphEl.classList.contains('is-visible')) collectTextRects(epigraphEl, textBoxes);
    var brand = document.querySelector('.brand');
    if (brand) collectTextRects(brand, textBoxes);
    var labels = document.querySelectorAll('.home-menu-item .hm-label');
    for (var i = 0; i < labels.length; i++) collectTextRects(labels[i], textBoxes);
  }

  function cacheLilyBoxes() {
    lilyBoxes.length = 0;
    var lily = document.getElementById('artLily');
    if (!lily) return;
    var svg = lily.querySelector('svg');
    if (!svg) return;
    var svgRect = svg.getBoundingClientRect();
    if (svgRect.width < 4 || svgRect.height < 4) return;
    var maxArea = svgRect.width * svgRect.height * 0.20;
    var shapes = svg.querySelectorAll('path, circle, ellipse, line, rect, use');
    for (var i = 0; i < shapes.length; i++) {
      var el = shapes[i];
      if (el.closest && el.closest('defs')) continue;           /* 跳过模板 defs */
      if (el.getAttribute('stroke-dasharray')) continue;        /* 跳过制图辅助虚线 */
      if (parseFloat(el.getAttribute('opacity') || '1') < 0.2) continue; /* 跳过极淡辅助线 */
      var r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      if (r.width * r.height > maxArea) continue;              /* 过滤大面积容器框 */
      var pad = 2.5;                                           /* 向内收紧 2–3px */
      lilyBoxes.push({ x: r.left + pad, y: r.top + pad, w: r.width - pad * 2, h: r.height - pad * 2 });
    }
  }

  function updateEpigraph() {
    if (!epigraphEl) return;
    var want = settled && !viewOpen && !modalOpen;
    if (want !== epigraphVisible) {
      epigraphVisible = want;
      epigraphEl.classList.toggle('is-visible', want);
    }
    if (want !== textCollisionActive) {
      textCollisionActive = want;
      cacheTextBoxes();
    }
  }

  cacheLilyBoxes();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () {
    cacheTextBoxes();
    cacheLilyBoxes();
  });

  /* ---- 核心可调常量（密度 / 速度 / 明度线宽 / 飞溅物理，后续微调集中改这里） ---- */
  var CFG = {
    /* —— 雨丝密度（已降至原约 44%，1080p 视口约 38–50 根） —— */
    tiltDeg: 9,                 /* 向右下倾斜角（度） */
    countBase: 44,              /* 1920 宽基准雨丝数（1080p ≈ 44 根） */
    countMin: 38,
    countMax: 50,
    farRatio: 0.40,             /* 远景细短雨丝占比（远景同样参与 100% 碰撞） */

    /* —— 雨丝速度（统一放缓约 50%，静谧滞空细雨节奏） —— */
    farSpeed: [260, 380],       /* 远景下落速度 px/s（原 [520,760]） */
    nearSpeed: [390, 540],      /* 主景下落速度 px/s（原 [780,1080]） */

    /* —— 雨丝形态与明度（整体调淡、收细） —— */
    farLen: [16, 30],
    nearLen: [36, 72],
    farWidth: 0.7,              /* 远景线宽 0.65–0.8px（原 0.8） */
    nearWidth: [0.85, 1.1],     /* 主景线宽 0.85–1.1px（原 [1.0,1.45]） */
    farAlpha: [0.07, 0.13],     /* 远景透明度（原 [0.16,0.28]） */
    nearAlpha: [0.18, 0.34],    /* 主景前端透明度（原 [0.45,0.72]） */

    /* —— 大花轮廓（碰撞命中半径） —— */
    lobeBase: 0.76,             /* rHit = R*(lobeBase + lobeAmp*|cos(lobeK*(θ-ωt))|) */
    lobeAmp: 0.24,
    lobeK: 4,
    flowerBandRatio: 0.12,      /* 外缘花瓣碰撞捕获带厚度（占 rHit 比例，0.08→0.12 放宽） */

    /* —— 飞溅短水丝（主景 2–3 道，远景 1–2 道且等比调淡调细，保留层次） —— */
    splashPerHit: [2, 3],       /* 主景飞溅水丝数 */
    splashPerHitFar: [1, 2],    /* 远景飞溅水丝数 */
    splashLen: [6, 18],
    splashLenFar: [4, 12],
    splashWidth: [0.6, 1.1],
    splashWidthFar: [0.45, 0.62],
    splashArc: [2, 6],
    splashAlpha: [0.18, 0.34],  /* 飞溅透明度，匹配主景雨丝亮度 */
    splashAlphaFar: [0.08, 0.16],
    splashLife: [0.35, 0.75],
    splashGravity: 880,         /* 飞溅重力 px/s² */
    splashDrag: 1.1,            /* 空气阻力 */
    vnIdle: [35, 70],           /* 慢转法向弹开速度 px/s */
    vnSpin: [240, 360],         /* 高速离心径向增量 px/s（保持甩得更远反差） */
    vtFactor: 0.027,            /* 切向甩水 vt = omega * vtFactor * R * rand */

    fadeTo: 0.35,                /* 子视图/弹窗时整体透明度 */

    /* —— 文字 / 百合碰撞（100% 命中，极细斜向短水丝，不画圆点） —— */
    textSplashCount: [1, 3],    /* 主景每次生成的极细短水丝数 */
    textSplashCountFar: [1, 2], /* 远景每次生成的极细短水丝数 */
    textSplashLen: [4, 9],      /* 短水丝长度 px */
    textSplashWidth: [0.55, 0.85],
    textSplashWidthFar: [0.35, 0.55],
    textSplashAlpha: [0.16, 0.30],
    textSplashAlphaFar: [0.07, 0.15],
    textSplashLife: [0.55, 0.95],
    textSplashGravity: 260,     /* 慢重力 px/s² */
    textSplashVx: [-36, 36],    /* 左右微偏初速度 px/s */
    textSplashVy: [100, 160]    /* 斜向上初速度 px/s（决定弹开约 20–48px） */
  };

  var TILT = CFG.tiltDeg * Math.PI / 180;
  var DIR_X = Math.sin(TILT);
  var DIR_Y = Math.cos(TILT);

  function rand(a, b) { return a + Math.random() * (b - a); }
  function randArr(a) { return rand(a[0], a[1]); }
  function clampNum(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  var rain = [];
  var splashes = [];

  function makeDrop() {
    var far = Math.random() < CFG.farRatio;
    var speed = far ? randArr(CFG.farSpeed) : randArr(CFG.nearSpeed);
    return {
      x: rand(-W * 0.25, W * 1.1),
      y: rand(-H * 0.7, H * 0.2),
      vx: DIR_X * speed,
      vy: DIR_Y * speed,
      len: far ? randArr(CFG.farLen) : randArr(CFG.nearLen),
      width: far ? CFG.farWidth : randArr(CFG.nearWidth),
      alpha: far ? randArr(CFG.farAlpha) : randArr(CFG.nearAlpha),
      far: far
    };
  }

  function resetDrop(d) {
    d.x = rand(-W * 0.25, W * 1.1);
    d.y = rand(-H * 0.7, -d.len);
  }

  function seedRain() {
    var count = Math.round(CFG.countBase * (W / 1920));
    count = clampNum(count, CFG.countMin, CFG.countMax);
    rain.length = 0;
    for (var i = 0; i < count; i++) rain.push(makeDrop());
  }
  seedRain();

  window.addEventListener('resize', function () {
    measure();
    resizeCanvas();
    seedRain();
    cacheTextBoxes();
    cacheLilyBoxes();
    if (isSettled()) applyRotation();
  });

  /* ---- 雨丝与飞溅物理 ---- */
  function stepDrop(d, dt) {
    d.x += d.vx * dt;
    d.y += d.vy * dt;
    if (d.y - d.len > H + 40 || d.x > W + 60 || d.x < -80) resetDrop(d);
  }

  function spawnSplash(px, py, theta, far) {
    var nx = Math.cos(theta), ny = Math.sin(theta);  /* 向外法向 */
    var tx = -Math.sin(theta), ty = Math.cos(theta); /* 顺时针切向（顺大花旋转） */
    var spin = clampNum(Math.abs(omega) / DEG_HOVER, 0, 1.4);
    var range = far ? CFG.splashPerHitFar : CFG.splashPerHit;
    var n = range[0] + ((Math.random() * (range[1] - range[0] + 1)) | 0);
    for (var i = 0; i < n; i++) {
      var vn = randArr(CFG.vnIdle) + spin * spin * randArr(CFG.vnSpin);
      var vt = omega * CFG.vtFactor * R * rand(0.8, 1.1);
      var sn = rand(0.85, 1.15);
      var st = rand(0.75, 1.2);
      splashes.push({
        x: px, y: py,
        vx: nx * vn * sn + tx * vt * st + (Math.random() - 0.5) * 26,
        vy: ny * vn * sn + ty * vt * st - rand(30, 90),
        len: randArr(far ? CFG.splashLenFar : CFG.splashLen),
        width: randArr(far ? CFG.splashWidthFar : CFG.splashWidth),
        arc: randArr(CFG.splashArc),
        alpha: randArr(far ? CFG.splashAlphaFar : CFG.splashAlpha),
        life: randArr(CFG.splashLife),
        age: 0
      });
    }
  }

  /* 文字 / 百合碰撞：原雨丝消散，落点生成 1–3 道（远景 1–2 道）极细短水丝，
     斜向上/左右微偏轻轻弹开后随慢重力下坠淡出（不画圆点）。 */
  function spawnTextSplash(px, py, far) {
    var range = far ? CFG.textSplashCountFar : CFG.textSplashCount;
    var n = range[0] + ((Math.random() * (range[1] - range[0] + 1)) | 0);
    for (var i = 0; i < n; i++) {
      splashes.push({
        x: px, y: py,
        vx: randArr(CFG.textSplashVx) * (far ? 0.8 : 1),
        vy: -randArr(CFG.textSplashVy),
        len: randArr(CFG.textSplashLen),
        width: randArr(far ? CFG.textSplashWidthFar : CFG.textSplashWidth),
        arc: 0,
        alpha: randArr(far ? CFG.textSplashAlphaFar : CFG.textSplashAlpha),
        life: randArr(CFG.textSplashLife),
        age: 0,
        grav: CFG.textSplashGravity
      });
    }
  }

  function checkTextHit(d) {
    if (!textBoxes.length) return false;
    for (var i = 0; i < textBoxes.length; i++) {
      var b = textBoxes[i];
      if (d.x >= b.x && d.x <= b.x + b.w && d.y >= b.y && d.y <= b.y + b.h) return true;
    }
    return false;
  }

  function hitText(d) {
    spawnTextSplash(d.x, d.y, d.far);
    resetDrop(d);
  }

  function checkLilyHit(d) {
    if (!lilyBoxes.length) return false;
    for (var i = 0; i < lilyBoxes.length; i++) {
      var b = lilyBoxes[i];
      if (d.x >= b.x && d.x <= b.x + b.w && d.y >= b.y && d.y <= b.y + b.h) return true;
    }
    return false;
  }

  function hitLily(d) {
    spawnTextSplash(d.x, d.y, d.far);
    resetDrop(d);
  }

  function checkFlowerHit(d) {
    var dx = d.x - CX, dy = d.y - CY;
    var distSq = dx * dx + dy * dy;
    if (distSq > R * R) return false;
    var dist = Math.sqrt(distSq);
    if (dist < 1) return false;
    var theta = Math.atan2(dy, dx);
    var rHit = R * (CFG.lobeBase + CFG.lobeAmp * Math.abs(Math.cos(CFG.lobeK * (theta - angleRad))));
    if (dist > rHit) return false;
    /* 只在上半圆或外缘接触带触发，避免雨丝穿心后在底部凭空反弹 */
    var band = Math.max(6, rHit * CFG.flowerBandRatio);
    if (dy > 0 && dist < rHit - band) return false;
    return true;
  }

  function hitFlower(d) {
    var dx = d.x - CX, dy = d.y - CY;
    var theta = Math.atan2(dy, dx);
    var rHit = R * (CFG.lobeBase + CFG.lobeAmp * Math.abs(Math.cos(CFG.lobeK * (theta - angleRad))));
    var px = CX + Math.cos(theta) * rHit;
    var py = CY + Math.sin(theta) * rHit;
    spawnSplash(px, py, theta, d.far);
    resetDrop(d);
  }

  function updateRain(dt) {
    for (var i = 0; i < rain.length; i++) {
      var d = rain[i];
      stepDrop(d, dt);
      /* 单根雨丝每周期只撞击一次并重置；文字更靠前/更高，优先于百合与大花 */
      if (textCollisionActive && checkTextHit(d)) hitText(d);
      else if (settled && checkLilyHit(d)) hitLily(d);
      else if (settled && checkFlowerHit(d)) hitFlower(d);
    }
  }

  function updateSplashes(dt) {
    for (var i = splashes.length - 1; i >= 0; i--) {
      var s = splashes[i];
      var drag = Math.max(0, 1 - CFG.splashDrag * dt);
      s.vx *= drag;
      s.vy *= drag;
      s.vy += (s.grav || CFG.splashGravity) * dt;
      s.x += s.vx * dt;
      s.y += s.vy * dt;
      s.age += dt;
      s.alpha *= Math.max(0, 1 - dt * 0.7);
      if (s.age >= s.life || s.alpha <= 0.01 || s.y > H + 40) splashes.splice(i, 1);
    }
  }

  function drawRain() {
    rainCtx.setTransform(DPR, 0, 0, DPR, 0, 0);
    rainCtx.clearRect(0, 0, W, H);
    rainCtx.lineCap = 'round';

    for (var i = 0; i < rain.length; i++) {
      var d = rain[i];
      var sp = Math.sqrt(d.vx * d.vx + d.vy * d.vy) || 1;
      var ux = d.vx / sp, uy = d.vy / sp;
      var hx = d.x, hy = d.y;
      var tx = hx - ux * d.len, ty = hy - uy * d.len;
      var g = rainCtx.createLinearGradient(hx, hy, tx, ty);
      g.addColorStop(0, 'rgba(232,236,232,' + (d.alpha * atmoAlpha).toFixed(3) + ')');
      g.addColorStop(1, 'rgba(232,236,232,0)');
      rainCtx.strokeStyle = g;
      rainCtx.lineWidth = d.width;
      rainCtx.beginPath();
      rainCtx.moveTo(hx, hy);
      rainCtx.lineTo(tx, ty);
      rainCtx.stroke();
    }

    for (var j = 0; j < splashes.length; j++) {
      var s = splashes[j];
      var sp2 = Math.sqrt(s.vx * s.vx + s.vy * s.vy) || 1;
      var ux2 = s.vx / sp2, uy2 = s.vy / sp2;
      var sx = s.x, sy = s.y;
      var tx2 = sx - ux2 * s.len, ty2 = sy - uy2 * s.len;
      var a = s.alpha * atmoAlpha;
      var g2 = rainCtx.createLinearGradient(sx, sy, tx2, ty2);
      g2.addColorStop(0, 'rgba(232,236,232,' + a.toFixed(3) + ')');
      g2.addColorStop(1, 'rgba(232,236,232,0)');
      rainCtx.strokeStyle = g2;
      rainCtx.lineWidth = s.width;
      rainCtx.beginPath();
      rainCtx.moveTo(sx, sy);
      rainCtx.lineTo(tx2, ty2);
      rainCtx.stroke();

      if (s.age < 0.16 && s.arc > 0) {
        var arcA = a * (1 - s.age / 0.16) * 0.45;
        rainCtx.strokeStyle = 'rgba(232,236,232,' + arcA.toFixed(3) + ')';
        rainCtx.lineWidth = Math.max(0.5, s.width * 0.6);
        rainCtx.beginPath();
        rainCtx.arc(sx, sy, s.arc, 0, Math.PI * 1.7);
        rainCtx.stroke();
      }
    }
  }

  /* ---- 主循环 ---- */
  var rafId = null;
  var last = 0;
  var backdropEl = null;
  var frameCount = 0;
  var settled = false;
  var atmoAlpha = 1;
  var angleRad = 0;

  function refreshFlags() {
    frameCount++;
    viewOpen = !!(contentEl && contentEl.classList.contains('view-open'));
    if (!backdropEl && (frameCount % 20 === 0)) {
      backdropEl = document.querySelector('.lr-backdrop');
    }
    modalOpen = !!(backdropEl && backdropEl.classList.contains('is-open'));
    if (viewOpen || modalOpen) hoverActive = false;
  }

  function update(dt) {
    refreshFlags();
    settled = isSettled();
    updateEpigraph();

    var targetSpeed = DEG_IDLE;
    var targetGlow = 0;
    var targetDim = 0;

    if (viewOpen || modalOpen) {
      targetSpeed = DEG_IDLE;
      targetGlow = 0;
      targetDim = 0.6;
    } else if (hoverActive) {
      targetSpeed = DEG_HOVER;
      targetGlow = 1;
      targetDim = 0;
    }

    if (settled) {
      var rate = 1 - Math.exp(-dt * 2.6);
      omega += (targetSpeed - omega) * rate;
      angle = (angle + omega * dt) % 360;
      angleRad = angle * Math.PI / 180;
      glow += (targetGlow - glow) * (1 - Math.exp(-dt * 3.0));
      dim += (targetDim - dim) * (1 - Math.exp(-dt * 3.0));
      applyRotation();
      applyGlow();
    } else {
      clearRotation();
      clearGlow();
      angleRad = 0;
    }

    var targetAtmo = (viewOpen || modalOpen) ? CFG.fadeTo : 1;
    atmoAlpha += (targetAtmo - atmoAlpha) * (1 - Math.exp(-dt * 3.0));

    updateRain(dt);
    updateSplashes(dt);
  }

  function render() {
    drawRain();
  }

  function loop(now) {
    rafId = requestAnimationFrame(loop);
    if (document.hidden) { last = now; return; }
    var dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    update(dt);
    render();
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) last = performance.now();
  });

  last = performance.now();
  rafId = requestAnimationFrame(loop);

})();
