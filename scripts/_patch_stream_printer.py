from pathlib import Path

path = Path("src/coding_agent/ui/stream.py")
text = path.read_text(encoding="utf-8")
start = text.index("class _StreamPrinter:")
end = text.index("\ndef _chunk_text(")
new_class = '''class _StreamPrinter:
    """Owns console layout for reasoning + assistant text.

    Assistant answers are rendered **once in place** as Markdown when a message
    is complete. Content tokens are buffered (not printed as raw markdown source)
    so users never see a plain dump followed by a second rendered block.
    """

    def __init__(self, activity: _ActivityLine) -> None:
        self.activity = activity
        self.reasoning_open = False
        self.answer_open = False
        self.streamed_answer = False
        self.streamed_reasoning = False
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []
        self._printed_complete_texts: set[str] = set()
        self._token_streamed_msg_ids: set[str] = set()
        self._open_msg_id: str | None = None
        self._open_answer_parts: list[str] = []
        self._markdown_rendered_ids: set[str] = set()

    def _stop_activity(self) -> None:
        self.activity.stop()

    def close_reasoning(self) -> None:
        if self.reasoning_open:
            console.print()
            self.reasoning_open = False

    def close_answer(self) -> None:
        """Seal token buffer without printing raw text."""
        self.answer_open = False
        if self._open_msg_id:
            self._token_streamed_msg_ids.add(self._open_msg_id)

    def write_reasoning(self, text: str) -> None:
        if not text:
            return
        self._stop_activity()
        self.close_answer()
        if not self.reasoning_open:
            console.print()
            console.print("[dim italic]reasoning[/dim italic]:")
            self.reasoning_open = True
            self.streamed_reasoning = True
        console.print(Text(text, style="dim"), end="")
        try:
            console.file.flush()
        except Exception:  # noqa: BLE001
            pass
        self.reasoning_buf.append(text)

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        """Buffer content tokens; do not print raw markdown source.

        Live feedback is the activity spinner (composing answer).
        Markdown is rendered once when the full message arrives.
        """
        if not text:
            return
        self.close_reasoning()
        self.activity.update("model", "composing answer...")
        if not self.answer_open:
            self.answer_open = True
            self._open_answer_parts = []
            self._open_msg_id = msg_id
        elif msg_id and self._open_msg_id and msg_id != self._open_msg_id:
            self.flush_buffered_answer()
            self.answer_open = True
            self._open_answer_parts = []
            self._open_msg_id = msg_id
        elif msg_id and not self._open_msg_id:
            self._open_msg_id = msg_id

        self.answer_buf.append(text)
        self._open_answer_parts.append(text)
        if msg_id:
            self._token_streamed_msg_ids.add(msg_id)

    def _print_markdown_answer(self, text: str, *, msg_id: str | None = None) -> None:
        """Single in-place markdown render for one assistant message."""
        text = text.strip()
        if not text:
            return
        if text in self._printed_complete_texts:
            return
        if msg_id and msg_id in self._markdown_rendered_ids:
            return

        self._stop_activity()
        self.close_reasoning()
        self.answer_open = False
        self._open_answer_parts = []
        self._open_msg_id = None

        console.print()
        console.print("[bold green]assistant[/bold green]:")
        console.print(render_markdown(text))
        try:
            console.file.flush()
        except Exception:  # noqa: BLE001
            pass

        self.answer_buf.append(text)
        self._printed_complete_texts.add(text)
        self.streamed_answer = True
        if msg_id:
            self._markdown_rendered_ids.add(msg_id)
            self._token_streamed_msg_ids.add(msg_id)

    def write_answer_complete(
        self,
        text: str,
        *,
        msg_id: str | None = None,
    ) -> None:
        """Render a finished assistant message once as Markdown (in place)."""
        text = text.strip()
        if not text:
            return
        self._print_markdown_answer(text, msg_id=msg_id)

    def flush_buffered_answer(self) -> None:
        """If tokens were buffered but no complete update arrived, render them."""
        buffered = "".join(self._open_answer_parts).strip()
        msg_id = self._open_msg_id
        self._open_answer_parts = []
        self.answer_open = False
        self._open_msg_id = None
        if buffered:
            self._print_markdown_answer(buffered, msg_id=msg_id)

    def finalize_line(self) -> None:
        self.close_reasoning()
        self.flush_buffered_answer()


'''
path.write_text(text[:start] + new_class + text[end:], encoding="utf-8")
print("replaced", start, end)
import ast

ast.parse(path.read_text(encoding="utf-8"))
print("syntax ok")
