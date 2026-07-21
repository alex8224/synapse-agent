from pathlib import Path

p = Path("src/coding_agent/ui/stream.py")
t = p.read_text(encoding="utf-8")

old1 = """        self._md_live = None
        self._pending_md_text = \"\"
"""
new1 = """        self._md_live = None
        self._pending_md_text = \"\"
        try:
            console.print()
        except Exception:  # noqa: BLE001
            pass
"""
if old1 in t:
    t = t.replace(old1, new1, 1)
    print("patched stop_md_live")
else:
    print("stop_md_live marker missing")

old2 = '''def print_info(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")'''
new2 = '''def print_info(message: str) -> None:
    # Leading newline avoids sticking to a Live markdown frame on Windows.
    console.print()
    console.print(f"[dim]{message}[/dim]")'''
if old2 in t:
    t = t.replace(old2, new2, 1)
    print("patched print_info")
else:
    print("print_info marker missing")

p.write_text(t, encoding="utf-8")
import ast
ast.parse(t)
print("syntax ok")
