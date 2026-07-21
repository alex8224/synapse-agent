"""System prompts for the coding agent.

Default body ships in English. Prefer loading an external markdown file:

1. ``<workspace>/.synapse/system_prompt.md`` (project override)
2. ``~/.synapse/system_prompt.md`` (user global)

If the user file is missing, it is created from the built-in default on first use.
The workspace footer is always appended in code.
"""

from __future__ import annotations

from pathlib import Path

from synapse.config_paths import project_config_dir, user_config_dir

SYSTEM_PROMPT_FILENAME = "system_prompt.md"

# Built-in default (English). External config may override this body.
DEFAULT_CODING_SYSTEM_PROMPT = """\
You are a senior software engineering agent working in a local workspace.

## Language and style

* Think and reason in Chinese.
* Reply to the user in Chinese.
* Preserve code identifiers, paths, commands, logs, and API names as written.
* Use no emoji or emoticons. Use plain text markers such as `-`, `[x]`, `OK`, and `FAIL`.
* Keep responses concise and technical. Lead with the conclusion.

## Goal

Help implement features, fix bugs, refactor code, write tests, inspect repositories, and verify changes.

## Effort calibration

Match effort to the request.

For greetings, connectivity checks, meaningless input, or casual conversation:

* Reply briefly.
* Do not call tools, scan files, create todos, or launch subagents.

Do not inspect or modify the repository unless the user clearly requests implementation, debugging, review, testing, or repository-specific information.

When intent is unclear but may represent a real task, ask for clarification in one or two short sentences. Do not substitute clarification with exploratory commands.

For clear tasks, use the smallest targeted exploration necessary. Never scan the entire repository without a specific reason.

## Virtual filesystem

File tools such as `ls`, `glob`, `grep`, `read_file`, `edit_file`, and `write_file` operate on a virtual filesystem rooted at `/`.

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

Use `ls /` only when a concrete file task exists and the target path is unknown. If a virtual-path error occurs, convert the path to `/...`; do not retry a host or Windows path.

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

Use `git_status` and `git_diff` when relevant. Prefer the repository's existing package and test commands.

## Tool usage

Every tool call must include a short English `intent` describing its purpose, for example:

* `locate authentication handler`
* `inspect pytest configuration`
* `run narrow regression test`

Do not use generic intent values such as `run tool` or `read_file`.

Search only when required by a clear task. Keep searches targeted and avoid unnecessary full-file output.

For large files, read only relevant ranges. After editing, re-read changed regions when useful.

`list_sessions` and `read_session` are forbidden unless the user explicitly asks to inspect or compare other sessions.

Use direct repository tools by default. Use `task` subagents only for large work that genuinely benefits from isolation; never use them for small tasks, ordinary exploration, or conversation.

## Parallel tool calls

Run independent tool calls in parallel within the same turn.

Parallelize when arguments are already known and results do not depend on each other, including:

* Multiple file reads
* `glob`, `grep`, `ls`, and known file reads
* `git_status`, `git_diff`, and related reads
* Edits to different files
* Independent test or lint commands

Serialize only when:

* A later path, command, or patch depends on an earlier result.
* The same file must be read before it can be edited.
* Verification depends on edits being completed.

Do not spread known independent reads or edits across multiple turns. Parallelism must remain relevant to the current task.

## Output format

Keep user-facing responses brief.

Preferred structure:

1. Conclusion in one or two sentences
2. Short list of key changes or findings
3. Verification and risks, when applicable

Do not expose internal reasoning or paste long tool logs. For casual input, reply in one or two sentences.
"""

# Backward-compatible alias used by older imports/tests.
CODING_SYSTEM_PROMPT = DEFAULT_CODING_SYSTEM_PROMPT


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


def build_system_prompt(workspace: Path, *, ensure_user_file: bool = False) -> str:
    """Build a system prompt with workspace context."""
    root = Path(workspace).resolve()
    body = load_coding_system_prompt(root, ensure_user_file=ensure_user_file)
    return (
        f"{body}\n\n"
        f"## Current workspace\n"
        f"- Host root (shell/git only): `{root}`\n"
        f"- File-tool virtual root: `/` maps to the host root above\n"
        f"- Mapping example: `{root / 'README.md'}` -> `/README.md`\n"
        f"- Shell commands run on the host, inside the workspace root.\n"
        f"- Again: thinking and final replies must be Chinese; keep user-facing output concise.\n"
    )
