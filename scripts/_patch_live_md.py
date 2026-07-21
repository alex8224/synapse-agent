from pathlib import Path

path = Path("src/coding_agent/ui/stream.py")
text = path.read_text(encoding="utf-8")
start = text.index("class _StreamPrinter:")
end = text.index("\ndef _chunk_text(")
new_class = r'''class _StreamPrinter:
    """Owns console layout for reasoning + assistant text.

    Assistant markdown is rendered **live in place**: each content token updates
    a Rich Live view of the accumulated Markdown. There is no raw-source dump
    and no second post-hoc panel when streaming already showed the answer.
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
        self._md_live: Live | None = None
        self._last_md_update = 0.0
        self._md_update_interval = 0.05  # ~20 fps; keeps Windows terminals responsive

    def _stop_activity(self) -> None:
        self.activity.stop()

    def close_reasoning(self) -> None:
        if self.reasoning_open:
            console.print()
            self.reasoning_open = False

    def _answer_group(self, text: str):
        from rich.console import Group

        body = text if text.strip() else "…"
        return Group(
            Text("assistant:", style="bold green"),
            render_markdown(body),
        )

    def _stop_md_live(self, *, final_text: str | None = None) -> None:
        """Stop live markdown view, optionally freezing on final_text."""
        live = self._md_live
        if live is None:
            return
        try:
            if final_text is not None and final_text.strip():
                live.update(self._answer_group(final_text))
                live.refresh()
            live.stop()
        except Exception:  # noqa: BLE001
            try:
                live.stop()
            except Exception:  # noqa: BLE001
                pass
        self._md_live = None

    def _update_md_live(self, text: str, *, force: bool = False) -> None:
        """Refresh in-place markdown render with accumulated text."""
        now = time.time()
        if (
            not force
            and self._md_live is not None
            and (now - self._last_md_update) < self._md_update_interval
        ):
            return
        self._last_md_update = now
        self._stop_activity()
        self.close_reasoning()
        if self._md_live is None:
            self._md_live = Live(
                self._answer_group(text),
                console=console,
                refresh_per_second=16,
                transient=False,
                vertical_overflow="visible",
            )
            self._md_live.start()
        else:
            try:
                self._md_live.update(self._answer_group(text))
            except Exception:  # noqa: BLE001
                # Recover from a dead Live context.
                self._md_live = None
                self._update_md_live(text, force=True)

    def close_answer(self) -> None:
        """Seal token buffer; keep live view until flush/complete."""
        self.answer_open = False
        if self._open_msg_id:
            self._token_streamed_msg_ids.add(self._open_msg_id)

    def write_reasoning(self, text: str) -> None:
        if not text:
            return
        # Reasoning and answer live views must not overlap.
        if self._md_live is not None or self._open_answer_parts:
            self.flush_buffered_answer()
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
        """Append a content token and re-render accumulated Markdown live."""
        if not text:
            return
        self.close_reasoning()
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

        self._open_answer_parts.append(text)
        if msg_id:
            self._token_streamed_msg_ids.add(msg_id)
        accumulated = "".join(self._open_answer_parts)
        self._update_md_live(accumulated)
        self.streamed_answer = True

    def _print_markdown_answer(self, text: str, *, msg_id: str | None = None) -> None:
        """Finalize one assistant message as Markdown (live freeze or one-shot)."""
        text = text.strip()
        if not text:
            return
        if text in self._printed_complete_texts:
            self._stop_md_live()
            return
        if msg_id and msg_id in self._markdown_rendered_ids:
            self._stop_md_live()
            return

        self._stop_activity()
        self.close_reasoning()
        self.answer_open = False
        self._open_answer_parts = []
        self._open_msg_id = None

        if self._md_live is not None:
            # Already streaming live → freeze on final text (no second block).
            self._stop_md_live(final_text=text)
        else:
            console.print()
            console.print(self._answer_group(text))
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
        """Complete an assistant message; freeze live markdown if active."""
        text = text.strip()
        if not text:
            return
        # Prefer complete text (authoritative) over partial token buffer.
        self._print_markdown_answer(text, msg_id=msg_id)

    def flush_buffered_answer(self) -> None:
        """Flush token buffer / live view when tools or reasoning interrupt."""
        buffered = "".join(self._open_answer_parts).strip()
        msg_id = self._open_msg_id
        self._open_answer_parts = []
        self.answer_open = False
        self._open_msg_id = None
        if buffered:
            self._print_markdown_answer(buffered, msg_id=msg_id)
        else:
            self._stop_md_live()

    def finalize_line(self) -> None:
        self.close_reasoning()
        self.flush_buffered_answer()


'''
path.write_text(text[:start] + new_class + text[end:], encoding="utf-8")
import ast

ast.parse(path.read_text(encoding="utf-8"))
print("ok", start, end)
