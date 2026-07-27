# 安装

## 前置要求

- **Python >= 3.12**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** 包管理器

## 方法一：安装为系统 CLI 工具（推荐）

项目已声明 console script，安装后任意目录直接使用 `synapse` 命令。

```powershell
# 可编辑安装（开发推荐，改代码立刻生效）
powershell -ExecutionPolicy Bypass -File scripts/install.ps1

# 或手动
uv tool install --editable --force .
```

安装后即可使用：

```bash
synapse tui -w .
synapse run "查看当前仓库结构并总结" -w .
```

仓库根目录的 `synapse.cmd` 是一个薄启动器，优先级：PATH 上的入口 > `.venv\Scripts` > `uv run`。

## 方法二：本地 venv 开发

```bash
# 同步依赖
uv sync

# 使用 venv 入口（Windows）
.\.venv\Scripts\synapse.exe tui -w .

# 或模块入口
uv run python -m synapse tui -w .

# 兼容写法
uv run synapse chat -w .
```

## 卸载

```powershell
uv tool uninstall synapse
# 或
powershell -ExecutionPolicy Bypass -File scripts/install.ps1 -Uninstall
```

## 升级

```bash
# 如果通过 uv tool install 安装
uv tool install --editable --force .

# 如果通过 venv 使用
git pull && uv sync
```
