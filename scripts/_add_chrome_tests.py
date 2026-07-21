from pathlib import Path

p = Path("tests/test_tui_sink.py")
text = p.read_text(encoding="utf-8")
if "def test_chrome_helpers_match_grok_style_labels" not in text:
    text = text.rstrip() + """


def test_chrome_helpers_match_grok_style_labels():
    assert format_token_count(14_000) == "14K"
    assert format_token_count(392_832) == "393K"
    assert short_model_name("openai:gpt-4.1") == "gpt-4.1"
    assert short_workspace_label(r"F:\\project\\agent\\autoagents\\py-agent") == (
        "autoagents/py-agent"
    )
    raw = (
        "finished in 38.0s | tools=6 | token_stream=on | "
        "tokens: 394106 (in=392832 out=1274)"
    )
    assert soften_turn_footer(raw) == "Worked for 38.0s."
"""
    p.write_text(text + "\n", encoding="utf-8", newline="\n")
    print("added")
else:
    print("exists")
