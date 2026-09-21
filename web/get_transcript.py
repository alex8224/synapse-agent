import json
import sys
from pathlib import Path
from typing import Any

from synapse.sessions.transcript import (
    load_messages_from_sqlite_file,
    message_to_export_dict,
    split_messages_by_turns,
)


def export_turn(index: int, turn: list[Any]) -> dict[str, Any]:
    """把一个轮次投影为 `{turn_index, user, assistant, tools}` 导出结构。"""
    exported = [message_to_export_dict(m) for m in turn]
    user = ''.join(d.get('content') or '' for d in exported if d.get('role') == 'human').strip()
    assistant = '\n\n'.join(
        d.get('content') or ''
        for d in exported
        if d.get('role') == 'ai' and (d.get('content') or '').strip()
    ).strip()
    tools = [
        {'name': c.get('name') if isinstance(c, dict) else getattr(c, 'name', '')}
        for m in turn
        for c in (getattr(m, 'tool_calls', None) or [])
    ]
    return {'turn_index': index + 1, 'user': user, 'assistant': assistant, 'tools': tools}


cp, tid = Path(sys.argv[1]), sys.argv[2]
msgs = load_messages_from_sqlite_file(cp, tid)
turns = split_messages_by_turns(msgs)
res = [export_turn(i, t) for i, t in enumerate(turns)]
print(json.dumps(res))