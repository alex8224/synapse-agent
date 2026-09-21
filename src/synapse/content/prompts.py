"""System prompts for the coding agent.

Default body ships in English. Prefer loading an external markdown file:

1. ``<workspace>/.synapse/system_prompt.md`` (project override)
2. ``~/.synapse/system_prompt.md`` (user global)

If the user file is missing, it is created from the built-in default on first use.
The workspace footer is always appended in code.
"""
from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

from synapse.content.prompt_sections import (
    STABLE,
    SYSTEM_TARGET,
    PromptSection,
    render_system_prompt,
)
from synapse.settings.config_paths import project_config_dir, user_config_dir

SYSTEM_PROMPT_FILENAME = "system_prompt.md"

# Built-in default (English). External config may override this body.
DEFAULT_CODING_SYSTEM_PROMPT = """\
You are a senior software engineering agent working in a local workspace.

## Goal

Help implement features, fix bugs, refactor code, write tests, inspect repositories,
and verify changes.

## Effort calibration

Match effort to the request.

For greetings, connectivity checks, meaningless input, or casual conversation:

* Reply briefly.
* Do not call tools, scan files, create todos, or launch subagents.

Do not inspect or modify the repository unless the user clearly requests
implementation, debugging, review, testing, or repository-specific information.

When intent is unclear but may represent a real task, ask for clarification
in one or two short sentences. Do not substitute clarification with exploratory commands.

For clear tasks, use the smallest targeted exploration necessary.
Never scan the entire repository without a specific reason.

## Virtual filesystem

File tools such as `find_files`, `search_files`, `read_file`, `edit_file`, `patch`, and `write_file`
operate on a virtual filesystem rooted at `/`.

Valid paths:

* `/`
* `/README.md`
* `/src/app.py`
* `/tests`

File-tool paths must:

* Start with `/`.
* Be relative to the workspace root.
* Never use Windows drive paths, host absolute paths, or paths without a leading slash.

The real host workspace path may only be used by shell or git commands.

Use `find_files` with a narrow pattern only when a concrete file task exists and the target path
is unknown. Do not call the hidden DeepAgents tools `ls`, `glob`, or `grep`.
If a virtual-path error occurs, convert the path to `/...`;
do not retry a host or Windows path.

## Workspace rules

* Stay within the workspace unless explicitly asked otherwise.
* Read relevant files before editing when practical.
* Never invent file contents or repository facts.
* Prefer small, reversible changes over broad rewrites.
* Never expose secrets, credentials, private keys, or `.env` contents.
* Avoid destructive actions unless explicitly requested.

## Workflow

For a clear coding or debugging task:

1. Understand the request and clarify only critical ambiguity.
2. Locate the relevant files with targeted searches.
3. Read only the necessary files or line ranges.
4. Use `write_todos` only for genuinely multi-step, multi-file work likely to span multiple turns.
5. Apply focused edits.
6. Run the narrowest useful test, lint, typecheck, or build command.
7. Diagnose failures and iterate until successful or clearly blocked.
8. Finish with:

   * What changed
   * How it was verified
   * Remaining risks, if any

Prefer the repository's existing package and test commands.

## Tool usage

Every tool call must include a short English `intent` describing its purpose, for example:

* `locate authentication handler`
* `inspect pytest configuration`
* `run narrow regression test`

Do not use generic intent values such as `run tool` or `read_file`.

Search only when required by a clear task.
Keep searches targeted and avoid unnecessary full-file output.

For `search_files`, use `pattern` to match file contents and `glob` only as an optional
include filter for file paths. Prefer a narrow `path`; omit `glob` when `path` already names a
file or small directory. A value such as `**/*.py` includes Python files—it does not express
cache-directory exclusions. Built-in ignore rules already skip common caches and build artifacts.

For large files, read only relevant ranges. After editing, re-read changed regions when useful.

Prefer `patch` for normal multi-line changes to existing files. Use `edit_file` for a small exact
replacement when `old_string` is unique, or set `replace_all=true` intentionally. Use `write_file`
only to create a new file; do not replace an existing file wholesale when `patch` or `edit_file`
can preserve unrelated content.

`search_session` and `read_session` are forbidden unless the user explicitly
asks to inspect or compare other sessions.

Use direct repository tools by default. Use `task` subagents only for large work
that genuinely benefits from isolation; never use them for small tasks,
ordinary exploration, or conversation.

## Parallel tool calls

Run independent tool calls in parallel within the same turn.

Parallelize when arguments are already known and results do not depend on each other, including:

* Multiple file reads
* `find_files` and related file reads
* `execute` and related reads
* Edits to different files
* Independent test or lint commands

Serialize only when:

* A later path, command, or patch depends on an earlier result.
* The same file must be read before it can be edited.
* Verification depends on edits being completed.

Do not spread known independent reads or edits across multiple turns.
Parallelism must remain relevant to the current task.

## Output format

Keep user-facing responses brief.

Preferred structure:

1. Conclusion in one or two sentences
2. Short list of key changes or findings
3. Verification and risks, when applicable

Do not expose internal reasoning or paste long tool logs.
For casual input, reply in one or two sentences.
"""

# Backward-compatible alias used by older imports/tests.
CODING_SYSTEM_PROMPT = DEFAULT_CODING_SYSTEM_PROMPT

# Non-overridable rules appended after any external prompt body. External
# ``system_prompt.md`` files may replace ``DEFAULT_CODING_SYSTEM_PROMPT`` but
# never this section, so critical file-tool path rules survive distribution
# and cannot be lost by user-level overrides. Subagent system prompts get the
# same rules appended at compile time (``compile_task_specs``), because
# subagents do not see the main agent's prompt.
MANDATORY_CODING_RULES = """\
## File-tool paths (mandatory)

File-tool paths for `read_file`, `search_files`, `find_files`, `edit_file`, `patch`,
and `write_file` must:

* Start with `/`.
* Be relative to the workspace root.
* Never use Windows drive paths, host absolute paths, or paths without a leading slash.

The real host workspace path may only be used by shell or git commands.
If a virtual-path error occurs, convert the path to `/...`; do not retry a host or Windows path.
"""

TOOL_INTENT_RULES = """\
## Tool call intent

Every tool call must include a short English `intent` describing its purpose, for example:

* `locate authentication handler`
* `inspect pytest configuration`
* `run narrow regression test`

Do not use generic intent values such as `run tool` or `read_file`.
"""


def user_system_prompt_path() -> Path:
    """``~/.synapse/system_prompt.md``."""
    return user_config_dir() / SYSTEM_PROMPT_FILENAME


def project_system_prompt_path(workspace: Path | str | None = None) -> Path:
    """``<workspace>/.synapse/system_prompt.md``."""
    return project_config_dir(workspace) / SYSTEM_PROMPT_FILENAME


def ensure_user_system_prompt(*, force: bool = False) -> Path:
    """Ensure the user global prompt file exists; seed from built-in default.

    Returns the user prompt path. Does not overwrite an existing file unless
    ``force=True``.
    """
    path = user_system_prompt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if force or not path.is_file():
        path.write_text(DEFAULT_CODING_SYSTEM_PROMPT.strip() + "\n", encoding="utf-8")
    return path


def resolve_system_prompt_path(workspace: Path | str | None = None) -> Path | None:
    """Return the first existing external prompt file (project, then user)."""
    candidates = [
        project_system_prompt_path(workspace),
        user_system_prompt_path(),
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def load_coding_system_prompt(
    workspace: Path | str | None = None,
    *,
    ensure_user_file: bool = False,
) -> str:
    """Load prompt body from config file, else built-in default.

    When ``ensure_user_file`` is True and neither project nor user file exists,
    seed ``~/.synapse/system_prompt.md`` and load it.
    """
    path = resolve_system_prompt_path(workspace)
    if path is None and ensure_user_file:
        path = ensure_user_system_prompt()
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return DEFAULT_CODING_SYSTEM_PROMPT.strip()


def filesystem_tool_prompt(
    excluded_tools: Iterable[str] | None = None,
    *,
    hidden_builtin_search: bool = True,
    scope_rules: bool = False,
) -> str:
    """Build authoritative guidance matching the model-facing filesystem schemas.

    ``excluded_tools`` lists tools that are not available to the caller, so the
    guidance never advertises a tool the model cannot use. ``hidden_builtin_search``
    records whether the DeepAgents built-in ``ls``/``glob``/``grep`` tools are
    hidden (main agent and inheriting subagents) or still reachable (built-ins-only
    subagents). ``scope_rules`` is the subagent-only opt-in for a caller whose tool
    set is derived from policy: an excluded ``execute`` then also drops the "do not
    use ``execute`` as a substitute" note. The main agent keeps that note, so its
    prompt stays byte-stable. The rendered result is byte-identical to the
    historical string for the defaults.
    """
    blocked = {t.strip() for t in (excluded_tools or []) if t.strip()}
    if hidden_builtin_search:
        intro = (
            "This section overrides generic DeepAgents filesystem guidance. The model-facing `ls`, "
            "`glob`, and `grep` tools are hidden; never call them."
        )
    else:
        builtin_names = ("ls", "glob", "grep")
        active = [f"`{name}`" for name in builtin_names if name not in blocked]
        hidden = [f"`{name}`" for name in builtin_names if name in blocked]
        intro = (
            "This section overrides generic DeepAgents filesystem guidance. "
        )
        if not hidden:
            intro += "The DeepAgents built-in `ls`, `glob`, and `grep` tools are available."
        elif active:
            intro += "Available built-in search tools: " + ", ".join(active) + "."
        if hidden:
            intro += " Hidden built-in search tools: " + ", ".join(hidden) + "; never call them."
    rules: list[str] = [
        "## Active filesystem tools (authoritative)",
        intro,
    ]
    if "find_files" not in blocked:
        rules.append(
            "- Use `find_files(pattern, path, max_results, head_limit, offset)` to find paths "
            "by glob. Use a narrow `path` or `pattern`; do not scan the whole workspace "
            "without a reason."
        )
    if "search_files" not in blocked:
        rules.extend(
            [
                "- Use `search_files(pattern, path, glob, output_mode, max_results, head_limit, "
                "offset, context_lines, case_insensitive)` to search file contents. `pattern` is a "
                "ripgrep-compatible regex.",
                "- `search_files.glob` is an optional include-only path filter relative to `path`; "
                "it cannot express exclusions.",
                "- Omit `search_files.glob` when `path` already names a file or "
                "sufficiently narrow directory.",
                "- Common ignored caches and build artifacts are skipped by built-in ignore rules.",
            ]
        )
    if "read_file" not in blocked:
        rules.append(
            "- Use `read_file(file_path, offset, limit)` for bounded text reads. `offset` is "
            "zero-based; use pagination for large files."
        )
    if "patch" not in blocked:
        rules.append(
            "- Prefer `patch(file_path, patch)` for ordinary multi-line edits to an existing file; "
            "pass only unified-diff hunks beginning with `@@`."
        )
    if "edit_file" not in blocked:
        rules.append(
            "- Use `edit_file(file_path, old_string, new_string, replace_all)` only for a small "
            "exact replacement. `old_string` must be unique unless `replace_all` is true."
        )
    if "write_file" not in blocked:
        rules.append(
            "- Use `write_file(file_path, content)` to create a new file, not for routine edits "
            "to an existing file."
        )

    # Note on execute substitute
    non_execute_file_ops = {
        "find_files",
        "search_files",
        "read_file",
        "edit_file",
        "write_file",
        "patch",
    }
    active_file_ops = non_execute_file_ops - blocked
    # For a scope-aware caller (subagent) the note only matters when it can
    # actually run shell commands; the main agent keeps it unconditionally so its
    # prompt stays byte-identical to the pre-refactor layout.
    if active_file_ops and (not scope_rules or "execute" not in blocked):
        active_names = ", ".join(sorted(active_file_ops))
        rules.append(
            f"- Do not use `execute` as a substitute for file operations when active tools "
            f"({active_names}) can perform the operation."
        )

    return "\n".join(rules) + "\n"


def _shell_prompt(shell_executable: str) -> str:
    """Build non-overridable syntax guidance for the configured host shell."""
    shell = shell_executable.strip() or ("pwsh" if sys.platform == "win32" else "bash")
    name = shell.replace("\\", "/").rsplit("/", 1)[-1].lower()

    if name in {"pwsh", "pwsh.exe", "powershell", "powershell.exe"}:
        rules = (
            "- Use PowerShell syntax, not Bash syntax.\n"
            "- Do not use Bash heredocs such as `<<EOF` or `python - <<'PY'`.\n"
            "- For multiline input, use a PowerShell here-string, `python -c`, or a "
            "temporary script file.\n"
            "- Do not assume Bash, WSL, or Git Bash is available.\n"
        )
    elif name in {"bash", "bash.exe", "sh", "sh.exe"}:
        rules = "- Use Bash/POSIX shell syntax for `execute` commands.\n"
    elif name in {"cmd", "cmd.exe"}:
        rules = (
            "- Use Windows cmd.exe syntax, not Bash or PowerShell syntax.\n"
            "- Do not use Bash heredocs or PowerShell here-strings.\n"
        )
    else:
        rules = (
            "- Use syntax supported by this configured shell; do not assume Bash, PowerShell, "
            "or cmd semantics.\n"
        )

    return f"## Shell environment\n- The `execute` tool uses `{shell}`.\n{rules}"


def build_workspace_block(
    workspace: Path | str,
    *,
    shell_available: bool = True,
    scope_rules: bool = False,
) -> str:
    """Render the shared ``## Current workspace`` block.

    The host root, the virtual-root mapping, and the shell note are identical
    for the main agent and for subagents. ``scope_rules`` is the subagent-only
    opt-in that adds the workspace-containment / secret-handling rules and the
    shell path convention; the main agent's coding body already states both rules,
    so emitting them here as well would duplicate them *and* change the main
    agent's byte-stable prompt (invalidating its prompt cache prefix).
    ``shell_available=False`` is only honoured together with ``scope_rules``: the
    main agent keeps its historical shell line in every configuration.
    """
    root = Path(workspace).resolve()
    lines = [
        "## Current workspace",
        f"- Host root (shell/git only): `{root}`",
        "- File-tool virtual root: `/` maps to the host root above",
        f"- Mapping example: `{root / 'README.md'}` -> `/README.md`",
    ]
    if scope_rules:
        lines.append(
            "- Stay within this workspace unless the user explicitly authorizes another location."
        )
        lines.append("- Never expose secrets, credentials, private keys, or `.env` contents.")
    if scope_rules and not shell_available:
        lines.append("- Shell commands are not available to you in this context.")
        return "\n".join(lines)
    lines.append("- Shell commands run on the host, inside the workspace root.")
    if scope_rules:
        lines.append(
            "- In shell commands, use `.` for this working directory, not `/`; "
            "the file-tool virtual root is not a shell path."
        )
    return "\n".join(lines)


def build_environment_sections(
    workspace: Path | str,
    *,
    shell_executable: str | None = None,
    excluded_tools: Iterable[str] | None = None,
    shell_available: bool = True,
    hidden_builtin_search: bool = True,
    scope_rules: bool = False,
) -> list[PromptSection]:
    """Build the reusable workspace / filesystem / shell sections.

    These sections describe the shared execution environment and are the same
    building blocks for the main agent and for subagents. They never load or
    create the main ``system_prompt.md`` body; the caller supplies its own body
    (main agent: the external/default coding prompt; subagent: its definition).

    ``scope_rules`` selects the scope-aware (subagent) rendering: the shell
    section and the ``execute`` note follow the *effective* tool set, and the
    workspace block carries the containment rules. The default rendering is the
    historical main-agent one, byte-identical to the pre-refactor prompt.
    """
    root = Path(workspace).resolve()
    excluded = frozenset(excluded_tools or ())
    if scope_rules:
        shell_available = shell_available and "execute" not in excluded
    effective_shell = shell_executable or ("pwsh" if sys.platform == "win32" else "bash")
    sections = [
        PromptSection(
            name="Workspace",
            source="workspace",
            content=build_workspace_block(
                root,
                shell_available=shell_available,
                scope_rules=scope_rules,
            ),
            cache_hint=STABLE,
            injection_target=SYSTEM_TARGET,
        ),
        PromptSection(
            name="Filesystem Tools",
            source="filesystem_tools",
            content=filesystem_tool_prompt(
                excluded,
                hidden_builtin_search=hidden_builtin_search,
                scope_rules=scope_rules,
            ),
            cache_hint=STABLE,
            injection_target=SYSTEM_TARGET,
        ),
    ]
    if shell_available:
        sections.append(
            PromptSection(
                name="Shell",
                source="shell",
                content=_shell_prompt(effective_shell),
                cache_hint=STABLE,
                injection_target=SYSTEM_TARGET,
            )
        )
    return sections


def build_scope_limits_section(
    *,
    read_only: bool = False,
) -> PromptSection:
    """Render the subagent ``## Scope limits`` section.

    Describe tool-level restrictions without claiming that an allowed shell or
    another tool's internals are sandboxed. Missing capabilities must be reported
    rather than worked around. The shell capability is stated by the workspace
    block instead (see :func:`build_workspace_block`), so it is not repeated here.
    The section never echoes raw configuration.
    """
    lines = ["## Scope limits"]
    if read_only:
        lines.append(
            "- File-editing tools are unavailable. Do not use other tools to bypass "
            "these restrictions."
        )
    lines.append(
        "- Tool-name restrictions are enforced at call time. This is not an OS sandbox "
        "and does not restrict the internals of an allowed shell command or other tool. "
        "If the task requires a capability you do not have, report the limitation clearly "
        "instead of attempting a workaround."
    )
    return PromptSection(
        name="Scope Limits",
        source="scope_limits",
        content="\n".join(lines),
        cache_hint=STABLE,
        injection_target=SYSTEM_TARGET,
    )


def build_system_prompt_sections(
    workspace: Path,
    *,
    ensure_user_file: bool = False,
    shell_executable: str | None = None,
    excluded_tools: Iterable[str] | None = None,
) -> list[PromptSection]:
    """Split the coding system prompt into named, cache-hint-tagged sections.

    Every section is ``stable``: the list is built once per agent build and the
    rendered result is byte-identical to the pre-registry single-string prompt.
    Request-time sections (project instructions, memory, environment) are added
    later in the middleware chain, not here. The environment sections come from
    the shared :func:`build_environment_sections` helper so subagents reuse the
    exact same workspace / filesystem / shell guidance. The main agent keeps the
    default (non-``scope_rules``) rendering, so its prompt stays byte-identical.
    """
    root = Path(workspace).resolve()
    body = load_coding_system_prompt(root, ensure_user_file=ensure_user_file)
    return [
        PromptSection(
            name="Coding Body",
            source="body",
            content=body,
            cache_hint=STABLE,
            injection_target=SYSTEM_TARGET,
        ),
        PromptSection(
            name="Mandatory Rules",
            source="mandatory_rules",
            content=MANDATORY_CODING_RULES,
            cache_hint=STABLE,
            injection_target=SYSTEM_TARGET,
        ),
        *build_environment_sections(
            root,
            shell_executable=shell_executable,
            excluded_tools=excluded_tools,
        ),
    ]


def build_system_prompt(
    workspace: Path,
    *,
    ensure_user_file: bool = False,
    shell_executable: str | None = None,
    excluded_tools: Iterable[str] | None = None,
) -> str:
    """Build a system prompt with workspace and effective host-shell context."""
    return render_system_prompt(
        build_system_prompt_sections(
            workspace,
            ensure_user_file=ensure_user_file,
            shell_executable=shell_executable,
            excluded_tools=excluded_tools,
        )
    )