---
name: new-book
description: 当用户要求根据 notes/ 目录下的读书笔记（.md）生成新的知识地图 HTML 页面并更新 books.json 索引时使用此技能。
---

# 新增书籍知识地图标准流程

请严格遵守项目规则，根据用户当前消息中 `@` 指定的读书笔记文件（`notes/*.md`），按顺序执行以下操作：

1. **读取母版与笔记**：完整读取 `templates/map-template.html` 的代码结构和用户指定的 `notes/*.md` 原文。严禁修改 `notes/` 目录下的任何文件。
2. **生成新地图文件**：在 `maps/` 目录下创建一个与笔记同名的 `.html` 文件（例如 `notes/abc.md` 对应 `maps/abc.html`）。
   - 必须原封不动保留 `templates/map-template.html` 的全部 `<style>` 样式、HTML 结构（含左右可拖动分屏 `resizer`）以及 `<script>` 下半部分的渲染与交互函数（含卡片文字水平居中与二次点击取消高亮逻辑）。
   - 仅替换 `<title>` 标签和 `<script>` 顶部的 `const MAP_DATA = { ... };` 对象。
   - 根据笔记内容提取 4 至 6 个核心模块，严格套用上下两排水平网格坐标公式（4节点 `x: 260, 700`；5节点上排 `150, 490, 830` 下排 `310, 670`；6节点上下排均为 `150, 490, 830`；上排 `y: 175`，下排 `y: 435`）与连线防重叠规则（往返双线设对称正数 `curve` 并错开 `labelT` 为 `0.28`/`0.72`；跨列长线设 `-135` 绕行），确保卡片文字居中且无连线交叉。
3. **更新全站书目索引**：读取 `books.json`，在数组末尾追加该书的条目（包含 `id`、`title`、`author`、`subtitle`、`tags`、`date`、`summary`、`url` 字段，其中 `url` 指向 `./maps/对应文件名.html`），保持原有格式与历史条目不变。
4. **自检**：检查生成的 HTML 语法与 `books.json` 格式无误，确保未改动 `index.html`、`templates/map-template.html` 及其他旧地图文件。