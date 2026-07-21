from pathlib import Path

p = Path(r"F:\project\agent\autoagents\py-agent\src\coding_agent\ui\tui.py")
text = p.read_text(encoding="utf-8")
marker = "def run_tui("
idx = text.rfind(marker)
if idx < 0:
    raise SystemExit("run_tui not found")
prefix = text[:idx]
fixed = prefix + '''def run_tui(
    *,
    settings: Any,
    thread_id: str | None = None,
    env_path: Path | None = None,
    project_root: Path | None = None,
) -> None:
    """Build agent and launch the Textual app."""
    root = project_root or Path.cwd()
    agent = build_coding_agent(settings, project_root=root)
    tid = thread_id or default_thread_id()
    app = CodingAgentApp(
        agent=agent,
        settings=settings,
        thread_id=tid,
        env_path=env_path,
    )
    app.run()
'''
p.write_text(fixed, encoding="utf-8", newline="\n")
compile(fixed, str(p), "exec")
print("fixed ok", len(fixed.splitlines()))
