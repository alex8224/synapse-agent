import sys, json
from pathlib import Path
from synapse.sessions.transcript import load_messages_from_sqlite_file, split_messages_by_turns, message_to_export_dict
cp, tid = Path(sys.argv[1]), sys.argv[2]
msgs = load_messages_from_sqlite_file(cp, tid)
turns = split_messages_by_turns(msgs)
res = [{'turn_index': i + 1, 'user': ''.join(d.get('content') or '' for m in t if (d := message_to_export_dict(m)).get('role') == 'human').strip(), 'assistant': '\n\n'.join(d.get('content') or '' for m in t if (d := message_to_export_dict(m)).get('role') == 'ai' and (d.get('content') or '').strip()).strip(), 'tools': [{'name': c.get('name') if isinstance(c, dict) else getattr(c, 'name', '')} for m in t for c in (getattr(m, 'tool_calls', None) or [])]} for i, t in enumerate(turns)]
print(json.dumps(res))