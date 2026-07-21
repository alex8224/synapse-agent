from pathlib import Path

p = Path("src/coding_agent/ui/stream.py")
t = p.read_text(encoding="utf-8")
old = """    final_text = \"\".join(printer.answer_buf).strip() or extract_last_ai_text(final)
    complete = extract_last_ai_text(final)
    if complete and len(complete) >= len(final_text):
        final_text = complete"""
new = """    # Prefer last AI message text; answer_buf holds already-rendered answers.
    complete = extract_last_ai_text(final)
    buffered = \"\".join(printer.answer_buf).strip()
    final_text = complete or buffered"""
if old not in t:
    idx = t.find("final_text =")
    print("not found, context:")
    print(repr(t[idx : idx + 300]))
    raise SystemExit(1)
p.write_text(t.replace(old, new, 1), encoding="utf-8")
print("ok")
