---
name: new-book
description: 当用户要求根据 notes/ 目录下的读书笔记（.md）生成新的知识地图 HTML 页面并更新 books.json 索引时使用此技能。
---

# 新增书籍知识地图标准流程

请严格遵守项目规则，根据用户当前消息中 `@` 指定的读书笔记文件（`notes/*.md`），按顺序执行以下操作。本技能只负责产出数据与调用注入函数，绝不手写 HTML/CSS。

## 唯一合法流程（硬性红线）

新书地图必须严格走「LLM 只输出结构化 JSON → Python 确定性防重叠布局 → 注入 `templates/map-template.html`」三步，绝不允许模型自由编写 HTML/CSS/JS：

1. **读取母版与笔记**：完整读取 `templates/map-template.html` 的代码结构和用户指定的 `notes/*.md` 原文。严禁修改 `notes/` 目录下的任何文件。
2. **产出结构化 JSON**：让模型只输出一个 `MAP_DATA` 结构的 JSON 对象（不含任何 Markdown 代码块或解释文字）。
3. **确定性布局**：节点坐标由 `scripts/pipeline.py` 的 `build_deterministic_layout` 统一计算（层级网格/蛇形递进，水平列步长 `COL_GAP = 440px`、垂直行步长 `ROW_GAP = 260px`、每行最多 `MAX_COLS = 3`），严禁手工写死任何 `x`/`y` 坐标。
4. **注入模板**：调用 `scripts/pipeline.py` 的 `inject_map(slug, map_data)`，或严格复刻「仅替换 `<title>` 与 `const MAP_DATA = { ... };`、其余一律不动」的注入方式生成 `maps/<slug>.html`。

生成的新地图必须与 `templates/map-template.html` 在 HTML 结构、CSS 样式与渲染 JS 上完全一致（仅 `<title>`、`MAP_DATA` 与书籍元信息不同）。定稿标准与禁止事项一律以 `docs/map-standard.md` 为准。

## MAP_DATA 数据结构（字段与字数限制写死）

- `meta`：`title`、`subtitle`、`overview`。
- `perspectives`：每项含 `id`、`label`、`legendLabel`、`lineClass`（取值 `both`/`primary`/`secondary`）。
- `nodes`（6-10 个核心节点）：
  - `title` ≤ 12 字；
  - `brief`（卡片副标题）≤ 16 字，只放精简锚点，完整深度解析写入 `subPoints`/`perspectivesNote` 或连线 `detail`；
  - `coreIdea` 20-40 字一句话核心结论；
  - `summary` 60-120 字；
  - `subPoints` / `perspectivesNote` 数组，每项 `{label, text}`；
  - 严禁手写 `x`/`y`，坐标由布局函数注入。
- `edges`：每项含 `id`、`source`、`target`、`label`（短）、`detail`（40-90 字）、`curve`（-200~200 整数，0 为直线）、`labelT`（0~1）、`perspective`。
- `steps`（4-6 步）：每项含 `title`、`description`、`activeNodes`、`activeEdges`。
- `readingGuide`（必填，不可省略）：
  - `coreQuestion`：2-3 句直白话交代核心问题；
  - `startNodeId`：指向逻辑起点节点；
  - `recommendedPath`：3-5 个按逻辑推进的推荐节点（每项 `{id, label}`）。

若模型漏掉 `startNodeId` 或 `recommendedPath`，`normalize_map_data` 会按「第一个节点 + 节点顺序」自动兜底补全；若 `brief` 超 16 字，会自动把完整原文前置保留到右侧正文、卡片仅留 16 字内锚点。

## 更新全站书目索引

读取 `books.json`，在数组末尾追加该书条目（`id`、`title`、`author`、`subtitle`、`tags`、`date`、`summary`、`url`，其中 `url` 指向 `./maps/<slug>.html`），保持原有格式与历史条目不变。

## 自检

- 检查生成的 HTML 语法与 `books.json` 格式无误；
- 确认未改动 `index.html`、`templates/map-template.html` 及其他旧地图文件；
- 确认地图页 `title` 为 `书名 — Librarium`、返回按钮为 `← Librarium`、左上角返回链接指向 `../index.html?settled=1`。