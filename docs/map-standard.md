# 知识地图定稿标准（Map Standard）

本文件是全站知识地图的唯一视觉与交互定稿标准，`templates/map-template.html` 是唯一母版。
所有未来新书地图必须 100% 复刻此风格；任何跨窗口协作一律以本文件为准。

## 1. 唯一母版

- 母版：`templates/map-template.html`。
- 生成规则：仅替换 `<title>`、书籍元信息与 `const MAP_DATA = { ... };`，其余 HTML 结构、CSS、渲染 JS 逐字节不动。
- 已定稿参照：`maps/rujiao-daojiao.html`、`maps/xifangzhexueshi.html`。

## 2. 配色（黑 / 白 / 灰 / 绿，单画布一体）

- 页面底色 `#141c1a`，卡片底 `#18211f`，雾绿 `#202c28`。
- 主文字 `#ede9df`，次文字 `#ced7d1`，弱化 `#8aa097`，苔绿主色 `#85a897`。
- 节点卡片磨砂：`rgba(32,44,40,0.80)` + 细描边 `rgba(182,206,196,0.28)`。
- 右侧阅读栏磨砂：`rgba(24,33,31,0.6)` + `backdrop-filter: blur(12px)`。

## 3. 核心设计参数

| 参数 | 值 |
| --- | --- |
| 节点卡片尺寸 | `NODE_W = 272` / `NODE_H = 120` |
| 卡片描边 | `1px`，圆角 `rx=2` |
| 布局列步长 | `COL_GAP = 440px`（≥440，硬性写死） |
| 布局行步长 | `ROW_GAP = 260px`（≥260，硬性写死） |
| 每行最多列数 | `MAX_COLS = 3` |
| 包围盒安全留白 | `SAFE_MARGIN = 140px`（含外拱弧线控制点） |
| 双向连线错开 | `BIDIR_OFFSET = 30` |
| 右侧正文字号 | `16.5px`，行高 `1.9` |
| 阻尼曲线 | `cubic-bezier(0.22, 1, 0.36, 1)` |

## 4. 交互与视觉要点

- 跨列连线自动外拱绕行：`chooseArcCurve` + `bezierCrossesAnyCard`，优先向上/向下外拱，绕开中间卡片。
- 双向连线：对称双弧错开（`BIDIR_OFFSET`），绝不重叠为一条线。
- 连线文字标签默认隐藏（`opacity: 0`），仅悬停或选中节点时淡入为极简圆角胶囊（`rx: 999`、`stroke-width: 0.75`、无粗框、柔光）。
- 渲染分层：连线层 `layer-edges` 永远在节点卡片层 `layer-nodes` 之下（先 append 连线，再 append 节点）。
- 起点徽标：`01 · 起点` 内嵌于起点卡片右上角（`.node-card-badge`），起点卡片微光描边。
- 自动包围盒留白：`fitIfNeeded` / `SAFE_MARGIN` 保证节点与外拱弧线控制点均在视野内。
- 右侧阅读栏：默认展示「新手导览 + 推荐路线 + 全部节点索引」，正文 16.5px 分层排版。
- 雾幕切页转场：纯 GPU 合成层（`transform: translateZ(0)` + `will-change: opacity, transform`），只过渡 `opacity` 与 `transform`。

## 5. 数据结构与字数限制

见 `.agents/skills/new-book/SKILL.md` 的「MAP_DATA 数据结构」。要点：

- `title` ≤ 12 字；`brief` ≤ 16 字（超长自动前置保留到正文）；`coreIdea` 20-40 字；`summary` 60-120 字。
- `readingGuide` 必填（`coreQuestion` + `startNodeId` + `recommendedPath` 3-5 个）。

## 6. 禁止事项清单

- 禁止直线穿透卡片（连线必须外拱绕行）。
- 禁止满屏常驻绿色方框标签（连线标签默认隐藏，仅悬停/选中淡入胶囊）。
- 禁止动态全屏 `backdrop-filter` 模糊（仅右侧面板等局部磨砂）。
- 禁止骑边框徽标（`01 · 起点` 徽标必须干净内嵌在卡片右上角）。
- 禁止手写节点 `x`/`y` 坐标（必须由 `build_deterministic_layout` 计算）。
- 禁止模型自由编写 HTML/CSS/JS（只允许输出 JSON，注入母版）。
- 禁止下调布局步长（`COL_GAP`/`ROW_GAP` 只许上调）。
- 禁止修改 `index.html`、`templates/map-template.html`、已有地图与 `notes/*.md`、`books.json` 既有数据。
