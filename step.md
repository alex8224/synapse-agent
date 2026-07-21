# TUI Cursor 重构进度

> 方案：`docs/tui-cursor-refactor.md`  
> 目标：按方案重构 TUI（组头 + 明细 + 预览），并保持 CLI 兼容  
> 更新：2026-03-22 + 后续（Phase 0–3 主路径完成；风险#1 历史折叠已修复）

---

## 总览

| Phase | 内容 | 状态 |
|---|---|---|
| 0 | `timeline.py` 纯模型 + 单测 | **完成** |
| 1 | StreamSink / stream tool 事件增强 | **完成** |
| 2 | TUI 组头 + 逐文件明细 | **完成** |
| 3 | live 工具面板 + Ctrl+T 折叠 + preview | **完成** |
| 4 | 抛光 / README 快捷键 + 历史折叠加固 | 接近完成（剩手工视觉） |

---

## 已落地

### Phase 0 — `src/synapse/ui/timeline.py`
- `ToolItem` / `ToolGroup` / label / summary / preview 截断
- 测试：`tests/test_timeline.py`

### Phase 1 — Sink / stream
- duck-type：`tool_item_started` / `tool_item_finished` / `tool_group_closed`
- CLI 旧路径保留
- 测试：`tests/test_stream_tool_items.py`

### Phase 2–3 — TUI
- 组头 `▾ Read N files…` + 明细 `◆ Read design.md`（basename 橙色）
- **live `#tools` 面板**：可原地刷新；`Ctrl+T` 折叠/展开
- 短 preview 行号面板；turn 结束冻结进 history RichLog
- Thought：`◆ Thought for Xs` + 摘要；`Ctrl+E` 展开全文
- 顶栏 tokens；干净 Markdown；`› Build anything`

### 验证
- `ruff check` 通过
- `pytest` **125 passed**

---

## 日志

### 2026-03-22
- [x] 方案 + `step.md`
- [x] Phase 0–2
- [x] Phase 3 live tools 面板 + Ctrl+T
- [x] 全量测试 65
- [x] README 快捷键补一句（可选）
- [ ] 手工 `coding-agent tui` 目视确认（需本机）

### 后续
- [x] 风险 #1 历史 turn 工具组冻结后折叠：改为从 #log DOM 反向查找最后 ToolGroupBlock / ThoughtBlock，支持冻结后 Ctrl+T/Ctrl+E 切换（历史块保持 widget 实例 + update 生效）
- [x] 顺带确认鼠标点击折叠已实现（on_click）
- 全量测试 125 passed + ruff 通过

---

## 验收对照

- [x] 多文件 = 组头 + 明细
- [x] label 含 basename
- [x] tool preview（截断）
- [x] Thought 可展开（Ctrl+E）
- [x] 工具组可折叠（Ctrl+T，支持历史 turn 冻结后）
- [x] 干净 Markdown
- [x] 单测 + ruff（125 passed）
- [x] README 快捷键说明
- [ ] 手工 TUI 一轮

---

## 残留风险

1. ~~历史 turn 内工具组冻结后不能再折叠（已修复：action 改用 DOM 查询最后 Block，支持冻结后 Ctrl+T 切换）~~
2. Thought 非 Collapsible 组件，是摘要 + 追加全文
3. ~~未做鼠标点击折叠（代码已有 on_click 支持；快捷键也增强为 DOM 查找）~~
4. 手工视觉验收依赖本机运行 TUI

---

## 关键文件

| 路径 | 说明 |
|---|---|
| `docs/tui-cursor-refactor.md` | 方案 |
| `step.md` | 本进度 |
| `src/synapse/ui/timeline.py` | 纯模型 |
| `src/synapse/ui/sink.py` | 契约 |
| `src/synapse/ui/stream.py` | 事件 |
| `src/synapse/ui/tui.py` | Textual |
| `tests/test_timeline.py` | 模型测 |
| `tests/test_tui_sink.py` | Sink 测 |
| `tests/test_stream_tool_items.py` | 契约测 |
