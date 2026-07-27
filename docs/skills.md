# Skills 插件

Skills 是 Synapse 的可扩展插件系统，位于项目 `skills/` 目录下。每个 Skill 是一个包含 `SKILL.md` 的目录，Agent 在需要时自动加载。

## Skills 目录结构

```
skills/
├── python-testing/
│   └── SKILL.md
└── python-memory-diagnostics/
    └── SKILL.md
```

## SKILL.md 格式

每个 `SKILL.md` 使用 YAML frontmatter + Markdown 正文：

```markdown
---
name: python-testing
description: How to run and write Python tests in this project with uv and pytest.
license: MIT
compatibility: Requires uv and pytest in a Python 3.12+ project.
allowed_tools: execute, run_tests, read_file, write_file, edit_file
---

# Python Testing Skill

## Running Tests
...
```

### Frontmatter 字段

| 字段 | 说明 |
|---|---|
| `name` | Skill 名称 |
| `description` | 简短描述（Agent 据此判断是否适用） |
| `license` | 许可证 |
| `compatibility` | 兼容性要求 |
| `allowed_tools` | 允许使用的工具列表（逗号分隔） |

## 内置 Skills

Synapse 自带两个 Skills：

| Skill | 说明 |
|---|---|
| `python-testing` | 使用 uv + pytest 运行和编写测试 |
| `python-memory-diagnostics` | 诊断 Python 进程内存问题 |

## 如何编写自定义 Skill

1. 在 `skills/` 下创建目录，如 `skills/my-skill/`
2. 创建 `skills/my-skill/SKILL.md`，填入 frontmatter 和正文
3. 正文使用 Markdown，Agent 会在匹配时完整读取

### 示例：自定义工具 Skill

```markdown
---
name: docker-ops
description: Docker container management: build, run, inspect, clean up.
license: MIT
compatibility: Requires Docker CLI installed and accessible from shell.
allowed_tools: execute
---

# Docker Operations Skill

## 构建镜像

在包含 Dockerfile 的目录下：
```bash
docker build -t <name>:<tag> .
```

## 运行容器
```bash
docker run -d --name <name> -p 8080:80 <image>
```
```

## 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `skills_paths` | `["skills"]` | Skills 目录路径列表 |

可通过环境变量或 `Settings` 配置多个 Skills 路径：

```bash
# 支持多个逗号分隔路径
export AGENT_SKILLS_PATHS="skills,./shared-skills"
```

## 工作原理

1. Agent 启动时扫描 `skills_paths` 下的所有 `SKILL.md` 文件
2. 解析 frontmatter 提取元信息，构建 Skill 目录
3. 当用户问题匹配某个 Skill 的 `description` 时，Agent 自动加载对应 `SKILL.md` 全文
4. Skill 内容作为系统提示的一部分注入，指导 Agent 行为

## 注意事项

- Skill 仅用于**指导行为**，不是沙箱 — `allowed_tools` 列表是声明性的
- Skill 正文中的命令由 Agent 自主决定是否执行
- 不要把密钥写入 SKILL.md
