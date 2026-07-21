from pathlib import Path
import re

p = Path("src/coding_agent/ui/stream.py")
t = p.read_text(encoding="utf-8")

# 1) clean print_info
t2 = re.sub(
    r"def print_info\(message: str\) -> None:\n(?:.*\n)*?    console\.print\(f\"\[dim\]\{message\}\[/dim\]\"\)\n",
    'def print_info(message: str) -> None:\n    console.print(f"[dim]{message}[/dim]")\n',
    t,
    count=1,
)
if t2 == t:
    # manual
    t = t.replace(
        '''def print_info(message: str) -> None:
    # Leading newline avoids sticking to a Live markdown frame on Windows.
    console.print()
    console.print(f"[dim]{message}[/dim]")
''',
        '''def print_info(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")
''',
    )
    print("print_info fixed manual")
else:
    t = t2
    print("print_info fixed re")

# 2) replace entire _stop_md_live
start = t.index("    def _stop_md_live")
end = t.index("    def _update_md_live")
new_stop = '''    def _stop_md_live(self, *, final_text: str | None = None) -> None:
        """Stop live markdown view, optionally freezing on final_text."""
        live = self._md_live
        if live is None:
            return
        try:
            freeze = final_text if final_text is not None else getattr(self, "_pending_md_text", "")
            if freeze is not None and str(freeze).strip():
                live.update(self._answer_group(str(freeze)))
                live.refresh()
            live.stop()
        except Exception:  # noqa: BLE001
            try:
                live.stop()
            except Exception:  # noqa: BLE001
                pass
        self._md_live = None
        self._pending_md_text = ""
        # Ensure subsequent prints start on a fresh line (Windows Live quirk).
        try:
            console.print()
        except Exception:  # noqa: BLE001
            pass

'''
t = t[:start] + new_stop + t[end:]

# 3) ensure __init__ has pending field
if "self._pending_md_text" not in t[t.index("def __init__") : t.index("def __init__") + 800]:
    t = t.replace(
        "self._md_update_interval = 0.05  # ~20 fps; keeps Windows terminals responsive\n",
        "self._md_update_interval = 0.05  # ~20 fps; keeps Windows terminals responsive\n"
        "        self._pending_md_text = \"\"\n",
        1,
    )
    print("added pending field")

p.write_text(t, encoding="utf-8")
import ast
ast.parse(t)
print("syntax ok")
