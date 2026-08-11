"""Prompt controller: input completion, history recall, and paste handling.

Owns the ``#prompt`` interaction state (completion session, paste
replacements, input history) that used to live directly on ``CodingAgentApp``.
The Textual host keeps event wiring (``@on(Input.Changed, ...)``, actions) and
forwards calls here, so this class is not a Widget and can be unit-tested
without a running Textual app.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from textual.screen import ModalScreen
from textual.widgets import Input, Static

from synapse.ui.user_turn import compress_paste_placeholder


@dataclass
class PromptState:
    """Completion / paste session state owned by the prompt controller."""

    complete_active_idx: int = 0
    complete_base_value: str = ""
    complete_applied: str | None = None
    complete_candidates: list[str] = field(default_factory=list)
    paste_replacements: dict[str, str] = field(default_factory=dict)
    # Last rendered set of [image#N] ids in the prompt, to avoid re-rendering
    # the preview on every keystroke.
    last_preview_ids: set[int] = field(default_factory=set)


class PromptController:
    """Completion, history and clipboard-paste behavior for the prompt."""

    def __init__(self, app: Any, input_history: Any, image_bank: Any) -> None:
        self._app = app
        self._input_history = input_history
        self._image_bank = image_bank
        self.state = PromptState()
        self._complete_context: Any = None
        self._complete_context_at = 0.0

    # -- helpers ------------------------------------------------------------

    def complete_ctx(self):
        """Slash-completion context (settings-driven)."""
        from synapse.commands.slash_complete import build_complete_context

        now = time.monotonic()
        if self._complete_context is None or now - self._complete_context_at >= 5.0:
            self._complete_context = build_complete_context(self._app.settings)
            self._complete_context_at = now
        return self._complete_context

    def _prompt_widget(self) -> Input:
        return self._app.query_one("#prompt", Input)

    def _hint_widget(self) -> Static:
        return self._app.query_one("#complete-hint", Static)

    # -- completion ----------------------------------------------------------

    def set_complete_hint(self, value: str) -> None:
        from synapse.commands.slash_complete import complete_at_line, complete_slash

        hint = self._hint_widget()
        st = self.state

        # ---- 先更新补全会话基准值 ----
        # 防御：value 有 @ 但 base_value 没有 → 强制更新
        if self._app.project_root and "@" in value and "@" not in (st.complete_base_value or ""):
            st.complete_base_value = value
            st.complete_active_idx = 0
        elif not st.complete_base_value:
            # 新会话
            st.complete_base_value = value
            st.complete_active_idx = 0
        elif st.complete_applied and st.complete_applied == value:
            # 补全已应用 → 保持 base_value 不变（Tab / 箭头导航中）
            pass
        elif value.startswith(st.complete_base_value):
            # 用户继续输入更多字符 → 更新 base_value 缩小匹配范围
            st.complete_base_value = value
            st.complete_active_idx = 0
        else:
            # 用户改变了前缀方向 → 重置
            st.complete_base_value = value
            st.complete_active_idx = 0

        # ---- 基于 base_value 计算候选列表 ----
        cands: list[str] = []
        if value.startswith("/"):
            cands = complete_slash(st.complete_base_value or value, self.complete_ctx())
        elif self._app.project_root and "@" in value:
            cands = complete_at_line(st.complete_base_value or value, self._app.project_root)

        if not cands:
            hint.update("")
            st.complete_candidates = []
            return

        # 多行下拉菜单渲染（最多 6 行）
        max_rows = 6
        active = st.complete_active_idx
        # 确保 active 在有效范围内
        if active >= len(cands):
            active = len(cands) - 1
        # 滚动窗口：尽量让 active 行保持可见
        if active >= max_rows:
            window_start = active - max_rows + 1
            shown = cands[window_start : window_start + max_rows]
            offset = window_start
        else:
            shown = cands[:max_rows]
            offset = 0

        lines: list[str] = []
        for i, c in enumerate(shown):
            idx = offset + i
            # 提取 @/command 尾部用于紧凑显示
            if "@" in c:
                at_pos = c.rfind("@")
                tail = c[at_pos:]
            elif c.startswith("/"):
                tail = c
            else:
                tail = c
            if idx == active:
                lines.append(f"[bold reverse] {tail} [/]")
            else:
                lines.append(f"  {tail}")
        if len(cands) > offset + max_rows:
            lines.append(f"  [dim]...+{len(cands) - offset - max_rows} more[/]")
        elif offset > 0:
            lines.append(f"  [dim]...+{offset} above[/]")
        hint.update("\n".join(lines))

    def apply_completion(self, line: str) -> None:
        prompt = self._prompt_widget()
        prompt.value = line
        prompt.cursor_position = len(line)
        self.state.complete_applied = line
        self.set_complete_hint(line)

    def complete_next(self) -> None:
        """Accept / cycle slash completions (Tab)."""
        from synapse.commands.slash_complete import complete_at_line, complete_slash

        prompt = self._prompt_widget()
        if not prompt.has_focus:
            return
        value = prompt.value or ""
        st = self.state

        # --- @ path completion ---
        if self._app.project_root and "@" in value:
            # ghost 首次接受
            ghost = getattr(prompt, "_suggestion", "") or ""
            if (
                not st.complete_applied
                and ghost
                and ghost != value
                and "@" in ghost
            ):
                cands = complete_at_line(st.complete_base_value or value, self._app.project_root)
                st.complete_active_idx = 0
                self.apply_completion_candidate(cands, 0)
                return

            # 循环候选
            cands = self.current_completion_cands()
            if cands:
                nxt_idx = (st.complete_active_idx + 1) % len(cands)
                self.apply_completion_candidate(cands, nxt_idx)
            return

        # --- / command completion ---
        if not value.startswith("/"):
            return
        ctx = self.complete_ctx()

        # ghost 首次接受
        ghost = getattr(prompt, "_suggestion", "") or ""
        if (
            not st.complete_applied
            and ghost
            and ghost.casefold().startswith(value.casefold())
            and ghost != value
        ):
            cands = complete_slash(st.complete_base_value or value, ctx)
            st.complete_active_idx = 0
            self.apply_completion_candidate(cands, 0)
            return

        # 循环候选
        cands = self.current_completion_cands()
        if cands:
            nxt_idx = (st.complete_active_idx + 1) % len(cands)
            self.apply_completion_candidate(cands, nxt_idx)

    def complete_prev(self) -> None:
        """Cycle slash completions backwards (Shift+Tab)."""
        prompt = self._prompt_widget()
        if not prompt.has_focus:
            return
        value = prompt.value or ""
        st = self.state

        # --- @ path completion (prev) ---
        if self._app.project_root and "@" in value:
            cands = self.current_completion_cands()
            if cands:
                nxt_idx = (st.complete_active_idx - 1) % len(cands)
                self.apply_completion_candidate(cands, nxt_idx)
            return

        # --- / command completion (prev) ---
        cands = self.current_completion_cands()
        if cands:
            nxt_idx = (st.complete_active_idx - 1) % len(cands)
            self.apply_completion_candidate(cands, nxt_idx)

    def focus_next(self) -> bool:
        """Tab: run completion for @/slash, or signal fallback to focus next.

        Returns True when completion handled the key; False when the host
        should fall back to ``screen.focus_next()``.
        """
        prompt = self._prompt_widget()
        if prompt.has_focus:
            value = prompt.value or ""
            if self._app.project_root and "@" in value:
                self.complete_next()
                return True
            if value.startswith("/"):
                self.complete_next()
                return True
        return False

    def focus_previous(self) -> bool:
        """Shift+Tab: run completion (prev) for @/slash, or signal fallback."""
        prompt = self._prompt_widget()
        if prompt.has_focus:
            value = prompt.value or ""
            if self._app.project_root and "@" in value:
                self.complete_prev()
                return True
            if value.startswith("/"):
                self.complete_prev()
                return True
        return False

    def show_completions(self) -> None:
        """List available slash completions (Ctrl+Space)."""
        from synapse.commands.slash_complete import complete_slash

        prompt = self._prompt_widget()
        value = prompt.value or ""
        if not value.startswith("/"):
            self._app.append_event("type / to start a slash command", "dim")
            return
        cands = complete_slash(value, self.complete_ctx())
        if not cands and " " in value.rstrip():
            parent = value.rstrip().rsplit(" ", 1)[0] + " "
            cands = complete_slash(parent, self.complete_ctx())
        if not cands:
            self._app.append_event("no completions", "yellow")
            return
        self._app.append_event("completions:", "dim")
        for c in cands[:20]:
            mark = "*" if c == value else " "
            self._app.append_event(f" {mark} {c}", "dim")
        if len(cands) > 20:
            self._app.append_event(f"  ... +{len(cands) - 20} more", "dim")

    def set_prompt_value(self, text: str) -> None:
        prompt = self._prompt_widget()
        prompt.value = text
        prompt.cursor_position = len(text)
        self.set_complete_hint(text)

    def current_completion_cands(self) -> list[str]:
        """Return candidates for the active completion session
        (always based on complete_base_value)."""
        from synapse.commands.slash_complete import complete_at_line, complete_slash

        base = self.state.complete_base_value or ""
        if self._app.project_root and "@" in base:
            return complete_at_line(base, self._app.project_root)
        if base.startswith("/"):
            ctx = self.complete_ctx()
            cands = complete_slash(base, ctx)
            if len(cands) <= 1 and " " in base.rstrip():
                cands = complete_slash(base.rstrip().rsplit(" ", 1)[0] + " ", ctx)
            return cands
        return []

    def apply_completion_candidate(self, cands: list[str], idx: int) -> None:
        """Apply the candidate at *idx* and refresh the dropdown."""
        if not cands:
            return
        prompt = self._prompt_widget()
        nxt = cands[idx % len(cands)]
        self.state.complete_candidates = cands
        self.state.complete_active_idx = idx
        prompt.value = nxt
        prompt.cursor_position = len(nxt)
        self.state.complete_applied = nxt
        self.set_complete_hint(nxt)

    # -- history -------------------------------------------------------------

    def history_up(self) -> None:
        """Recall older project input history / navigate completion (up)."""
        if isinstance(self._app.screen, ModalScreen):
            return
        prompt = self._prompt_widget()
        if not prompt.has_focus:
            return
        st = self.state

        # 补全菜单活跃时：将 up/down 重定向为菜单导航
        if st.complete_base_value:
            cands = self.current_completion_cands()
            if cands:
                st.complete_active_idx = (
                    st.complete_active_idx - 1
                    if st.complete_active_idx > 0
                    else len(cands) - 1
                )
                self.apply_completion_candidate(cands, st.complete_active_idx)
                return

        nxt = self._input_history.up(prompt.value or "")
        if nxt is not None:
            self.set_prompt_value(nxt)

    def history_down(self) -> None:
        """Recall newer project input history / navigate completion (down)."""
        if isinstance(self._app.screen, ModalScreen):
            return
        prompt = self._prompt_widget()
        if not prompt.has_focus:
            return
        st = self.state

        # 补全菜单活跃时：将 up/down 重定向为菜单导航
        if st.complete_base_value:
            cands = self.current_completion_cands()
            if cands:
                st.complete_active_idx = (
                    st.complete_active_idx + 1
                ) % len(cands)
                self.apply_completion_candidate(cands, st.complete_active_idx)
                return

        nxt = self._input_history.down(prompt.value or "")
        if nxt is not None:
            self.set_prompt_value(nxt)

    def add_history(self, text: str) -> None:
        try:
            self._input_history.add(text)
        except Exception:  # noqa: BLE001
            pass

    # -- paste ---------------------------------------------------------------

    def paste_clipboard(self) -> None:
        """Alt+V clipboard paste (image or text)."""
        from synapse.content.multimodal import read_clipboard

        try:
            result = read_clipboard()
        except Exception:  # noqa: BLE001
            self._app.append_event("clipboard read failed", "yellow")
            return

        if result.kind == "empty":
            self._app.append_event("clipboard empty", "dim")
            return

        if result.kind == "text":
            text = result.text or ""
            prompt = self._prompt_widget()
            if len(text) > 200 or "\n" in text or "\r" in text:
                prefix = text[:20].replace("\r", " ").replace("\n", " ").strip()
                placeholder = f"[{prefix}... {len(text)} chars]"
                self.state.paste_replacements[placeholder] = text
                old = prompt.value or ""
                prompt.value = old + placeholder
                self._app.append_event(
                    f"pasted text truncated: {len(text)} chars -> "
                    "placeholder (content preserved)",
                    "dim",
                )
            else:
                old = prompt.value or ""
                prompt.value = old + text
            prompt.focus()
            return

        if result.kind == "image":
            try:
                att = self._image_bank.add_bytes(
                    result.data, mime=result.mime, name=result.name
                )
            except Exception as exc:  # noqa: BLE001
                self._app.append_event(f"image rejected: {exc}", "yellow")
                return
            self._app.append_event(
                f"pasted {att.name} -> [image#{att.id}]", "dim"
            )
            prompt = self._prompt_widget()
            old = prompt.value or ""
            prompt.value = old + f" [image#{att.id}]"
            prompt.focus()

    def expand_paste(self, text: str) -> tuple[str, str]:
        """Expand paste placeholders for submission.

        Returns ``(full_text, display_text)``: full text keeps the original
        pasted content for the model, display text compresses placeholders for
        rendering. Clears the placeholder mapping.
        """
        display = text
        for placeholder, full_text in list(self.state.paste_replacements.items()):
            if placeholder in text:
                text = text.replace(placeholder, full_text)
                display = display.replace(
                    placeholder, compress_paste_placeholder(placeholder)
                )
        self.state.paste_replacements.clear()
        return text, display

    def clear_paste_replacements(self) -> None:
        self.state.paste_replacements.clear()

    # -- prompt change ---------------------------------------------------------

    def on_prompt_changed(self, value: str) -> None:
        """Handle ``Input.Changed`` on #prompt (paste + completion state)."""
        st = self.state
        # 清理已失效的粘贴占位符映射（用户编辑后占位符被破坏）
        if st.paste_replacements:
            stale = [p for p in st.paste_replacements if p not in value]
            for p in stale:
                del st.paste_replacements[p]
        # 清理 / 命令补全状态（但不影响 @ 补全会话）
        in_at_session = bool(
            self._app.project_root
            and "@" in value
            and st.complete_base_value
            and "@" in st.complete_base_value
        )
        if not value.startswith("/") and not in_at_session:
            st.complete_applied = None
            st.complete_candidates = []
            st.complete_active_idx = 0
            st.complete_base_value = ""
        elif st.complete_applied and not value.casefold().startswith(
            st.complete_applied[: max(1, len(value))].casefold()
        ):
            st.complete_applied = None
            st.complete_active_idx = 0
            st.complete_base_value = ""
        # 清理 @ 补全状态：当 value 不再包含 @ 或不再以已应用的补齐开头
        if st.complete_applied and "@" in st.complete_applied:
            if "@" not in value or not value.startswith(
                st.complete_applied[: max(1, len(value))]
            ):
                st.complete_applied = None
                st.complete_candidates = []
                st.complete_active_idx = 0
                st.complete_base_value = ""
        self.set_complete_hint(value)
        self._sync_image_preview(value)

    def _sync_image_preview(self, value: str) -> None:
        """Re-render the pending-image preview when the placeholder set changes."""
        bank = self._image_bank
        if not bank.items:
            return
        from synapse.content.multimodal import find_placeholders

        ids = set(find_placeholders(value))
        if ids == self.state.last_preview_ids:
            return
        self.state.last_preview_ids = ids
        try:
            self._app.refresh_image_preview()
        except Exception:  # noqa: BLE001 - app/widget not ready
            pass
