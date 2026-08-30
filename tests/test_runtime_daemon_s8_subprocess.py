"""Real child-process daemon tests are intentionally not part of S8.

All S8 lifecycle coverage uses in-process injected fakes, events, and barriers.
This keeps verification within the caller's event loop and cannot affect an
unrelated daemon or any other process.
"""
