# 项目永久规范

## 全站命名

全站品牌名称统一且仅使用 `Librarium`，严禁再出现“阅读图谱与笔记”“个人阅读图谱与笔记”及类似说明性中文副标题。

- 主页 `<title>` 与主标题只显示 `Librarium`，保持留白克制，不加中文口号或冗余标语。
- 地图页 `<title>` 统一为 `书名 — Librarium`，返回首页按钮统一写为 `← Librarium`。

## 视觉规范（纯代码深雾灰绿画廊体系，单画布一体）

全站仅使用黑、白、灰、绿四色系，严禁出现蓝、紫、红、黄等其他彩色（唯一例外：主页百合花蕊的克制冷锈红 `#7c4a3a` 作为暖色焦眼）。

### 主页（单画布景深一体 + 开场翻书成花）

- 整页是一块统一的深雾灰绿油画布：底色由深松炭绿 `#141c1a`、雾板岩绿 `#202c28` 与柔和灰绿光雾 `#2c3b36` 多层柔和暗雾渐变叠加，全屏顶层铺极细 SVG `feTurbulence` 肌理噪点（`opacity: 0.05`，`pointer-events: none`），底层保留 26-34 颗柔光浮尘（`canvas`）。
- 开场仪式（约 3.2s，`cubic-bezier(0.22,1,0.36,1)`，`perspective: 1400px`）：竖书正面翻开 → 书页扇形展开 → 沿 X 轴倾倒（`rotateX 0→82°`）→ 书页弧线绽放为多瓣白色花形 SVG → 减淡融入背景，沉淀为正中偏上的白色花型暗纹（`opacity: 0.14-0.20`，`mix-blend-mode: screen` + 径向羽化遮罩，`pointer-events: none`）；随后 `Librarium` 花体标题、分类导航与磨砂卡片错峰上浮显现。
- 开场交互：从 `maps/` 返回或 `?settled=1` 时跳过开场直接落定；播放中点击任意处快进落定；点击顶部 `Librarium` 标题可重播“翻书成花”。
- 居中单栏画廊：主体容器 `max-width: 820px` 居中；页眉正中大号花体 `Librarium`，下方极细渐隐横线过渡到居中纯文字分类导航（苔绿滑动底线 + 淡雅卷数统计）；底部只留极简 `LIBRARIUM` 落款。
- 书目卡片为磨砂玻璃：`background: rgba(30,42,38,0.42)`、`backdrop-filter: blur(18px) saturate(115%)`、高光描边 `rgba(235,232,224,0.16)`、柔和深景阴影，内边距 `32px 38px` 宽裕；悬停 500ms 阻尼上浮 4px + 玻璃边框泛光 + 鼠标追光径向光晕跟随 + 箭头顺滑伸展，其余卡片淡退聚焦。

### 地图与阅读页（深雾灰绿画廊，与主页同源）

- 思维导图画布区：深松灰绿雾底（`#141c1a` → `#1c2724` 微妙径向渐变 + 极细画布噪点），底层保留 14-18 颗极轻微光浮尘（`opacity 0.16-0.30`，`pointer-events: none`，严格位于节点与正文之下）与极轻微鼠标追光。
- 右侧笔记阅读区：半透明雾炭绿磨砂面板（`rgba(24,33,31,0.6)` + `backdrop-filter: blur(12px)`）；标题温润骨白 `#ede9df`（衬线体），正文柔和雾灰白 `#ced7d1`（行高 1.85），引用与高亮苔绿 `#85a897`，分隔线 `rgba(176, 196, 186, 0.16)`。
- 节点卡片为深雾绿半透明磨砂玻璃（`rgba(34, 46, 42, 0.82)`，细边框 `rgba(178, 198, 188, 0.22)`），文字清晰且内边距宽裕。
- 顶部导航与 `← Librarium` 返回按钮（指向 `../index.html?settled=1`）无边界融入背景，花体排印，悬停带丝滑箭头回退动效与苔绿微光。

## 字体排印（全站英文花体 + 中文衬线）

- 展示型英文花体（`Librarium` 主标题、返回链接 `← Librarium`、页脚落款等）：`"Pinyon Script", "Snell Roundhand", "Edwardian Script ITC", "Vivaldi", "French Script MT", "Gabriola", "Apple Chancery", cursive`，给予充足字号与字距。
- 混排与辅助英文（英文副标、作者外文名、页码数字、英文标签等）：`"Cormorant Garamond", "Gabriola", "Palatino Linotype", Georgia, serif`，与中文衬线自然衔接。
- 中文书名与正文使用本地衬线体栈：`"Source Han Serif SC", "Noto Serif SC", "Songti SC", "STSong", Georgia, serif`。
- 编号使用等宽数字（`font-variant-numeric: tabular-nums`）。

## 动效与转场

- 全站统一阻尼曲线 `cubic-bezier(0.22, 1, 0.36, 1)`。
- 雾幕切页转场：进入页面时全屏雾灰绿幕布 620ms 淡出消散；点击站内相对链接时 450ms 雾幕浮现后跳转；监听 `pageshow` 重置幕布，防止“后退”白屏。
- 鼠标光源与光点浮尘：鼠标作柔光源以 `requestAnimationFrame` + `lerp(0.06)` 驱动局部柔光提亮花影（`mix-blend-mode: soft-light`），靠近时花瓣柔和浮现骨白光泽；光点浮尘呼吸明暗、鼠标 180px 半径内增亮并轻盈避让。
- 地图页：节点悬停 380ms 阻尼微放大与苔绿柔光晕、点击聚焦缩放与光环、关联连线流光脉冲（`stroke-dashoffset`）、非关联元素 450ms 淡退至 `opacity: 0.22`、右侧正文 420ms 淡入上浮、resizer 悬停拖拽苔绿亮起。

## 技术约束

纯静态 HTML（原生 HTML/CSS/JS/SVG），严禁引入任何外部图片、CDN 脚本或构建工具；字体除 Google Fonts 花体（`Pinyon Script` / `Cormorant Garamond`，`display=swap` 非阻塞引入）外不引入其他第三方资源，离线时自动回退本地花体兜底栈。

## 数据分离

每张知识地图（`maps/*.html`）把全书节点、连线、视角与步进数据统一放在 script 顶部的 `const MAP_DATA = { ... };` 中，渲染逻辑与数据解耦；左上角保留指向 `../index.html` 的返回链接。

## 增量红线

以后根据新笔记添加新书地图时，只允许新建 `maps/新书.html` 并在 `books.json` 末尾追加条目，严禁重写或修改 `index.html`、`templates/map-template.html` 及已有的其他地图文件。新书地图必须完整沿用本规范的命名、配色、字体、动效、鼠标光源与雾幕转场。

## 文本风格

忠于原始笔记，使用简单平和的书面中文，主谓宾结构，去掉口语化寒暄、比喻、总结套话、防御性表述和“不是……而是……”句式。

## 硬性红线

- `notes/` 目录下的所有 `.md` 文件属于用户的只读原稿，任何情况下严禁修改、覆盖或重写 `notes/*.md`。
- 严禁修改 `books.json` 中的书目数据。
