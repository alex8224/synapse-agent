"""UI package.

- CLI rendering: Rich (``stream.RichStreamSink``)
- TUI rendering: Textual (``tui.TextualStreamSink``)
- Shared port: ``sink.StreamSink`` consumed by ``stream.stream_agent``
"""

from synapse.ui.sink import StreamSink

__all__ = ["StreamSink"]
