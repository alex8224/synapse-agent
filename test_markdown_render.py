"""Quick smoke-test for Rich Markdown rendering.

Run:  uv run python test_markdown_render.py
"""
from coding_agent.ui.stream import console, render_markdown
from rich.panel import Panel

samples: dict[str, str] = {
    "h1-h4": (
        "# Heading 1\n\n"
        "## Heading 2\n\n"
        "### Heading 3\n\n"
        "#### Heading 4"
    ),
    "bold/italic/strikethrough": (
        "This is **bold**, *italic*, ***bold-italic***, and ~~strikethrough~~."
    ),
    "inline code": (
        "Use the `print()` function or `pytest` command. "
        "Paths like `/src/a.py` should render as inline code."
    ),
    "fenced code block (py)": (
        "```python\n"
        "def hello(name: str) -> str:\n"
        '    """Say hello."""\n'
        '    return f"Hello, {name}!"\n'
        "```"
    ),
    "fenced code block (shell)": (
        "```bash\n"
        "uv run pytest -q tests/\n"
        "uv run ruff check .\n"
        "```"
    ),
    "fenced code block (no lang)": (
        "```\n"
        "plain text\n"
        "  indented\n"
        "```"
    ),
    "unordered list": (
        "- Item A\n"
        "- Item B\n"
        "  - nested B1\n"
        "  - nested B2\n"
        "- Item C"
    ),
    "ordered list": (
        "1. First step\n"
        "2. Second step\n"
        "   1. sub-step 2.1\n"
        "   2. sub-step 2.2\n"
        "3. Third step"
    ),
    "blockquote": (
        "> This is a blockquote.\n"
        "> It can span multiple lines.\n"
        ">\n"
        "> - with a list inside\n"
        "> - item 2"
    ),
    "horizontal rule": "above\n\n---\n\nbelow",
    "table": (
        "| Column A | Column B | Column C |\n"
        "|----------|----------|----------|\n"
        "| foo      | bar      | baz      |\n"
        "| 123      | 456      | 789      |"
    ),
    "link & autolink": (
        "[Rich library](https://github.com/Textualize/rich) and "
        "plain URL: https://www.python.org"
    ),
    "mixed real answer": (
        "## 执行结果\n\n"
        "`uv run pytest -q` 输出如下：\n\n"
        "```\n"
        "48 passed in 4.44s\n"
        "```\n\n"
        "总结：\n"
        "- 全部 48 个测试通过\n"
        "- 无 ruff 告警\n\n"
        "> **注意**: 请确保 `uv sync` 后再运行。"
    ),
}

for label, md in samples.items():
    console.rule(f"[bold cyan]{label}[/bold cyan]")
    console.print(render_markdown(md))
    console.print()
