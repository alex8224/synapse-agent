from pathlib import Path

p = Path("src/coding_agent/ui/stream.py")
t = p.read_text(encoding="utf-8")

# revert noisy print_info
old2 = '''def print_info(message: str) -> None:
    # Leading newline avoids sticking to a Live markdown frame on Windows.
    console.print()
    console.print(f"[dim]{message}[/dim]")'''
new2 = '''def print_info(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")'''
if old2 in t:
    t = t.replace(old2, new2, 1)
    print("reverted print_info")

# ensure _stop_md_live ends cleanly — find function and patch tail
marker = "def _stop_md_live"
idx = t.find(marker)
if idx < 0:
    raise SystemExit("no _stop_md_live")
# find end of function: next def at same indent
rest = t[idx:]
# crude: locate "self._md_live = None"
pos = t.find("self._md_live = None", idx)
if pos < 0:
    raise SystemExit("no assign")
# take until after pending clear
chunk = t[pos : pos + 200]
print("chunk", repr(chunk[:120]))
# replace first occurrence after idx of assignment block
old = None
for candidate in [
    '        self._md_live = None\n        self._pending_md_text = ""\n',
    "        self._md_live = None\n        self._pending_md_text = ''\n",
]:
    if candidate in t[idx:]:
        old = candidate
        break
if not old:
    # show nearby
    print(repr(t[pos : pos + 80]))
    raise SystemExit("pattern missing")
new = old + "        try:\n            console.print()\n        except Exception:  # noqa: BLE001\n            pass\n"
if "console.print()" not in t[pos : pos + 180]:
    t = t[:idx] + t[idx:].replace(old, new, 1)
    print("patched stop_md_live")
else:
    print("already has console.print after stop")

# stream finished stats: force leading blank line
old3 = '''    if result.tool_calls or result.elapsed_s >= 0.5:
        print_info(
            f"finished in {result.elapsed_s:.1f}s | tools={result.tool_calls} | "'''
# may use different separator
import re
m = re.search(r"if result\.tool_calls or result\.elapsed_s >= 0\.5:\n\s+print_info\(", t)
if m:
    insert_at = m.start()
    indent = "    "
    guard = indent + "console.print()  # separate from live markdown frame\n"
    if "separate from live markdown" not in t:
        t = t[:insert_at] + guard + t[insert_at:]
        print("patched finished separator")
    else:
        print("finished separator exists")
else:
    print("finished block not found")

p.write_text(t, encoding="utf-8")
import ast
ast.parse(t)
print("syntax ok")
