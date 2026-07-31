# Synapse 项目协作指南

## 当前仓库结构

| 路径                               | 主要职责                                                 |
| ---------------------------------- | -------------------------------------------------------- |
| `src/synapse/app/`                 | Agent 装配与 `AGENTS.md` 注入                            |
| `src/synapse/commands/`            | slash 命令解析、补全和命令结果                           |
| `src/synapse/content/`             | prompt、skills、输入历史和多模态内容处理                 |
| `src/synapse/integrations/`        | MCP、模型传输、Codex 导入、图片识别等外部集成            |
| `src/synapse/models/`              | 模型 profile、registry、配置解析与辅助逻辑               |
| `src/synapse/observability/`       | 启动与运行观测                                           |
| `src/synapse/runtime/`             | backend、middleware、压缩、steer、安全和异步运行时       |
| `src/synapse/sessions/`            | session、transcript、recap、取消修复与持久化             |
| `src/synapse/settings/`            | 分层配置路径、Settings schema 与加载逻辑                 |
| `src/synapse/tool_output/`         | 可逆工具输出检测、变换、存储和指标                       |
| `src/synapse/tools/`               | 注入 Agent 的自定义工具                                  |
| `src/synapse/ui/`                  | Textual TUI、stream、timeline、dialogs、topbar/bottombar |
| `tests/`                           | Python 测试；测试文件通常与领域模块对应                  |
| `rust/synapse-tool-compress-core/` | 可选 Rust/PyO3 原生工具输出压缩核心                      |
| `docs/`、`mkdocs.yml`              | 用户文档与 MkDocs 配置                                   |
| `.github/workflows/`               | CI、文档、Python Release 与原生 wheel 构建               |

## 架构与兼容性约定

- 将 `src/synapse/app/agent.py` 视为装配层，不要把具体领域算法继续堆入该文件。
- 新功能应落入对应领域包；跨领域装配放在 `app/` 或明确的 runtime middleware 中。
- `src/synapse/config.py` 是兼容导出层。新代码优先从 `synapse.settings` 导入，但不要无理由破坏旧导入路径。
- 各包 `__init__.py` 中的导出属于公共 API。移动实现时保留必要 re-export，并检查现有测试和扩展调用方。
- 配置采用用户层与项目层合并策略。修改 Settings 时同步检查：
  - `src/synapse/settings/schema.py`
  - `src/synapse/settings/config_paths.py`
  - `tests/test_config.py`
  - `tests/test_layered_config.py`
  - `README.md` 与 `docs/config.md` 中的用户文档
- `AGENTS.md` 由 `AgentMdMiddleware` 静态注入，独立于可写 memory。不要把它重新并入 memory 写回机制。

## 编码规范

- Ruff 配置是 Python 风格的唯一自动化基线：行宽 100，目标 Python 3.12，规则集 `E/F/I/B/UP`。
- 为新增或修改的公共函数、复杂状态转换和兼容分支补充类型标注。
- 捕获宽泛异常只用于明确的降级边界，并说明为何允许 fallback；不要静默吞掉核心业务错误。
- 避免无界读取、无界搜索和无界终端输出；对日志、工具结果和外部数据设置合理上限。
- 不在代码、测试快照、文档或终端输出中暴露 API key、token、`.env` 内容或用户私有配置。
- 修改用户可见行为时，同步更新相应 README/docs；修改内部实现但行为未变时，不制造无关文档改动。

## 6. Python 开发与测试

首次安装或依赖变化后执行：

```powershell
uv sync
```

验证遵循“最窄测试 → 相关领域测试 → 全量检查”的顺序。

### 6.1 针对性测试

```powershell
uv run --no-sync pytest tests/test_x.py -q
uv run --no-sync pytest tests/test_x.py::test_case_name -q
```

按改动领域优先选择相应测试，例如：

| 改动领域                | 优先测试                                                                 |
| ----------------------- | ------------------------------------------------------------------------ |
| settings/models         | `tests/test_config.py`、`tests/test_layered_config.py`、模型相关测试     |
| backend/safety/runtime  | `tests/test_backends.py`、`tests/test_safety.py`、对应 middleware 测试   |
| tool output/compression | `tests/test_tool_output.py`、`tests/test_tool_output_*`、请求压缩测试    |
| sessions/Codex import   | `tests/test_session_*`、`tests/test_transcript.py`、`tests/test_codex_*` |
| CLI/slash commands      | `tests/test_cli.py`、`tests/test_slash_*`                                |
| TUI/widgets/dialogs     | 对应 `test_tui_*`、`test_stream_*`、`test_dialogs.py` 和组件测试         |

### 6.2 全量检查

针对性测试通过后，根据改动风险运行：

```powershell
uv run --no-sync ruff check .
uv run --no-sync pytest -q
```

CI 会在 Windows/Linux、Python 3.12/3.13 上运行 lint 和测试。平台相关改动至少要覆盖当前本机，并在代码层面检查另一平台分支。

### 6.3 文档与构建

修改 `docs/`、`README.md` 或 `mkdocs.yml` 后运行：

```powershell
uv run --no-sync mkdocs build
```

修改打包配置、入口点或发布内容后可运行：

```powershell
uv build
```

## 7. Rust/PyO3 原生压缩核心

- `synapse-tool-compress-core` 是独立可选包，不是 Synapse 的必需开发依赖。
- Python 主程序必须在未安装原生 wheel 时正常工作，不能移除 `ImportError`/`OSError` fallback。
- 修改 `rust/synapse-tool-compress-core/` 时，至少运行：

```powershell
cargo test --manifest-path rust/synapse-tool-compress-core/Cargo.toml
cargo fmt --manifest-path rust/synapse-tool-compress-core/Cargo.toml --check
```

- 如修改 Python 绑定/API，再构建或安装本地扩展，并运行 `tests/test_tool_output.py` 及相关压缩测试。
- 保留 `src/headroom_port/` 中的 Apache-2.0 SPDX、来源归属、`LICENSE` 和 `NOTICE`；不要引入被明确排除的网络调用或模型下载依赖。
- 原生 wheel 由 `.github/workflows/native-compression-wheels.yml` 独立构建，tag 格式为 `synapse-tool-compress-core-v*`。

## 9. 发布流程

任何 `git push` 前必须询问用户：**“本次是否需要打 tag 发 Release？”**
如果用户确认不发布，只执行普通 push，不创建 tag。
如果用户确认发布：

1. 读取 `pyproject.toml` 的当前版本，并让用户确认或覆盖版本号。
2. 用 `git log` 对比上一个 `v*` tag 以来的变更。
3. 在 `CHANGELOG.md` 顶部新增 `## v{version}` 段落；标题必须与 tag 完全一致，并按新增功能、修复、工程改进等类别总结。
4. 如版本变化，更新 `pyproject.toml`；需要时同步 `uv.lock`。
5. 运行相关测试、Ruff 和 `uv build`，确认工作树内容正确。
6. 提交发布文件，提交信息使用：`release: bump to v{version}`。
7. 运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/release.ps1
```

8. 脚本会创建并推送 `v{version}` tag；`.github/workflows/release.yml` 会从 `CHANGELOG.md` 提取同名段落、执行 `uv build` 并创建 GitHub Release。

## 10. cdp_take_screenshot 工具使用说明

### 10.1 已知限制
- `filePath` 参数无法写入工作区目录（浏览器沙箱限制），所有指定 `filePath` 的尝试均会报 `Access denied`。
- 不指定 `filePath` 时，截图以文本格式（含 base64）自动存入 `/large_tool_results/call_xx_xxx`。

### 10.2 标准流程：截图 → 解码 → 识图

执行完 `cdp_take_screenshot`（不指定 `filePath`）后，按以下步骤操作：

#### Step 1 — 编写/复用解码脚本
如果工作区不存在 `/decode_screenshot.py`，创建它：
```python
import base64, re, sys, glob, os

files = glob.glob('large_tool_results/call_*')
if not files:
    print("No screenshot files found")
    sys.exit(1)

latest = max(files, key=os.path.getctime)
print(f"Processing: {latest}")

content = open(latest, 'r').read()
m = re.search(r"data='([^']+)'", content)
if not m:
    print("No base64 data found")
    sys.exit(1)

os.makedirs('.tmp', exist_ok=True)
out = f'.tmp/screenshot_{os.path.getmtime(latest)}.png'
open(out, 'wb').write(base64.b64decode(m.group(1)))
print(f"OK -> {out}")
```

#### Step 2 — 执行解码
```bash
python decode_screenshot.py
# 输出示例: OK -> .tmp/screenshot_1712345678.1234567.png
```

#### Step 3 — 调用识图
```bash
# 使用 Step 2 返回的文件名
describe_image(image_path="/.tmp/screenshot_1712345678.1234567.png", ...)
```

### 10.3 注意事项
- 每次截图都会在 `/large_tool_results/` 生成一个新文件，解码脚本自动选最新的。
- 输出到 `/.tmp/` 目录，该目录需加入 `.gitignore`。
- 不要在对话中手动执行多步解码，应直接调用解码脚本。
