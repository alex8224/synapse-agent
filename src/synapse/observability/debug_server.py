"""Lightweight HTTP debug inspector for LLM request/response inspection.

Starts a background HTTP server on ``127.0.0.1:9090`` (configurable) serving
a single-page app that displays captured model-call records in real time.

Usage::

    from synapse.observability.debug_server import DebugHttpServer

    server = DebugHttpServer(get_debug_store())
    server.start()
    server.open_browser()  # opens http://127.0.0.1:9090
"""

# ruff: noqa: E501  (embedded HTML / JS / CSS)

from __future__ import annotations

import dataclasses
import json
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from synapse.observability.llm_debug import DebugCaptureStore

_DEFAULT_HOST = "127.0.0.1"
_PORT_START = 9090
_PORT_END = 9100


def _find_free_port(start: int = _PORT_START, end: int = _PORT_END) -> int:
    """Find a free TCP port in [start, end); fall back to OS-assigned."""
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    # Exhausted: let the OS decide
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

# ---------------------------------------------------------------------------
# JSON encoder for DebugCaptureRecord
# ---------------------------------------------------------------------------


def _record_to_dict(record: Any, *, request_delta_start: int = 0) -> dict[str, Any]:
    """Convert a DebugCaptureRecord to a JSON-safe dict."""
    d = dataclasses.asdict(record)
    d["request_delta_start"] = request_delta_start
    # Preserve a bounded body for expandable message inspection.
    for msg in d.get("request_messages", []):
        content = msg.get("content_full", "")
        msg["content_full"] = content[:16_384]
        msg["content_truncated"] = len(content) > len(msg["content_full"])
    for msg in d.get("response_messages", []):
        content = msg.get("content_full", "")
        msg["content_full"] = content[:16_384]
        msg["content_truncated"] = len(content) > len(msg["content_full"])
    return d


def _record_to_raw_dict(record: Any) -> dict[str, Any]:
    """Return the complete bounded capture for explicit raw-record inspection."""
    return dataclasses.asdict(record)


def _message_identity(message: dict[str, Any]) -> tuple[Any, ...]:
    """Build a stable identity for common-prefix comparison between calls."""
    return (
        message.get("role"),
        message.get("name"),
        message.get("tool_call_id"),
        message.get("content_full"),
        tuple(
            (call.get("id"), call.get("name"), call.get("args"))
            for call in message.get("tool_calls", [])
        ),
    )


def _request_delta_start(records: list[Any], index: int) -> int:
    """Return count of unchanged leading request messages in this turn."""
    if index <= 0 or records[index - 1].turn_index != records[index].turn_index:
        return 0
    previous = records[index - 1].request_messages
    current = records[index].request_messages
    common = 0
    for previous_message, current_message in zip(previous, current, strict=False):
        if _message_identity(previous_message) != _message_identity(current_message):
            break
        common += 1
    return common


def _tool_pairs(records: list[Any], index: int) -> list[dict[str, Any]]:
    """Pair tool calls with tool-result messages from the current turn context."""
    turn_index = records[index].turn_index
    start = index
    while start > 0 and records[start - 1].turn_index == turn_index:
        start -= 1

    current_record = records[index]
    relevant_ids: set[str] = set()
    for message in [*current_record.request_messages, *current_record.response_messages]:
        relevant_ids.update(str(call.get("id") or "") for call in message.get("tool_calls", []))
        if message.get("role") == "tool":
            relevant_ids.add(str(message.get("tool_call_id") or ""))
    relevant_ids.discard("")

    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records[start : index + 1]:
        for message in [*record.request_messages, *record.response_messages]:
            for tool_call in message.get("tool_calls", []):
                call_id = str(tool_call.get("id") or "")
                if not call_id or call_id in calls:
                    continue
                calls[call_id] = {
                    "id": call_id,
                    "name": str(tool_call.get("name") or "unknown"),
                    "args": str(tool_call.get("args") or ""),
                    "result": None,
                }
                order.append(call_id)
            if message.get("role") != "tool":
                continue
            call_id = str(message.get("tool_call_id") or "")
            if not call_id:
                continue
            if call_id not in calls:
                calls[call_id] = {
                    "id": call_id,
                    "name": "(missing tool call)",
                    "args": "",
                    "result": None,
                }
                order.append(call_id)
            calls[call_id]["result"] = message.get("content_full", "")

    return [calls[call_id] for call_id in order if call_id in relevant_ids]


def _record_summary(record: Any, index: int) -> dict[str, Any]:
    """Return the small, polling-safe projection used by the record list."""
    request_messages = record.request_messages
    response_messages = record.response_messages
    has_tools = any(
        message.get("tool_calls") or message.get("role") == "tool"
        for message in [*request_messages, *response_messages]
    )
    return {
        "index": index,
        "turn_index": record.turn_index,
        "model_call_index": record.model_call_index,
        "usage": record.usage,
        "provider": record.provider,
        "model_name": record.model_name,
        "started_at": record.started_at,
        "duration_ms": record.duration_ms,
        "error": record.error,
        "request_count": len(request_messages),
        "response_count": len(response_messages),
        "has_tools": has_tools,
    }


# ---------------------------------------------------------------------------
# HTML page (embedded — no external files)
# ---------------------------------------------------------------------------

_LEGACY_PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM Debug Inspector</title>
<style>
:root {
  --bg: #1a1b1e;
  --bar: #2b2d31;
  --fg: #e8eaed;
  --dim: #9aa0a6;
  --muted: #5f6368;
  --user: #8ab4f8;
  --green: #81c995;
  --orange: #f4b183;
  --red: #f28b82;
  --border: #3c4043;
  --cyan: #78d9e6;
}
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:system-ui,monospace; background:var(--bg); color:var(--fg); height:100vh; display:flex; flex-direction:column; }
#toolbar { height:36px; background:var(--bar); display:flex; align-items:center; padding:0 12px; gap:12px; border-bottom:1px solid var(--border); flex-shrink:0; }
#toolbar .title { font-weight:bold; color:var(--user); }
#toolbar .status { font-size:12px; }
.toggle { display:flex; align-items:center; gap:6px; cursor:pointer; font-size:13px; user-select:none; }
.toggle .dot { width:32px; height:18px; border-radius:9px; background:var(--muted); position:relative; transition:background .2s; }
.toggle .dot::after { content:''; position:absolute; top:2px; left:2px; width:14px; height:14px; border-radius:50%; background:var(--bg); transition:left .2s; }
.toggle.on .dot { background:var(--green); }
.toggle.on .dot::after { left:16px; }
#main { display:flex; flex:1; overflow:hidden; }
#list { width:280px; flex-shrink:0; overflow-y:auto; border-right:1px solid var(--border); padding:4px 0; }
#list .item { padding:6px 10px; cursor:pointer; font-size:12px; border-left:3px solid transparent; }
#list .item:hover { background:var(--bar); }
#list .item.sel { background:var(--bar); border-left-color:var(--user); color:#fff; }
#list .item .turn { color:var(--user); font-weight:bold; }
#list .item .dur { color:var(--dim); }
#list .item .toks { color:var(--muted); }
#list .item .err { color:var(--red); }
#detail { flex:1; overflow-y:auto; padding:8px 14px; font-size:13px; line-height:1.5; }
#detail .sep { color:var(--orange); font-weight:bold; margin:8px 0 4px; }
#detail .section { color:var(--orange); font-weight:bold; }
#detail .role-s { color:var(--cyan); }
#detail .role-u { color:var(--user); font-weight:bold; }
#detail .role-a { color:var(--green); font-weight:bold; }
#detail .role-t { color:var(--dim); }
#detail .msg-body { white-space:pre-wrap; padding-left:12px; color:var(--fg); max-height:300px; overflow-y:auto; border-left:2px solid var(--border); margin:2px 0 8px; }
#detail .tc { color:var(--dim); padding-left:20px; font-size:12px; }
#detail .meta { color:var(--dim); font-size:12px; }
#empty { display:flex; align-items:center; justify-content:center; height:100%; color:var(--muted); font-size:14px; flex-direction:column; gap:8px; }
.hidden { display:none !important; }
</style>
</head>
<body>

<div id="toolbar">
  <span class="title">◆ LLM Debug Inspector</span>
  <label class="toggle" id="tgl" onclick="toggleCapture()">
    <span>Capture</span>
    <span class="dot"></span>
    <span id="tgl-label" class="status">OFF</span>
  </label>
  <span id="record-count" class="status" style="margin-left:auto;"></span>
</div>

<div id="main">
  <div id="list"></div>
  <div id="detail"><div id="empty">No records yet.<br><small>Enable Capture and run a turn.</small></div></div>
</div>

<script>
let records = [];
let selected = -1;
let pollTimer = null;

const $list = document.getElementById('list');
const $detail = document.getElementById('detail');
const $tgl = document.getElementById('tgl');
const $tglLabel = document.getElementById('tgl-label');
const $count = document.getElementById('record-count');
const $empty = document.getElementById('empty');

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  return r.ok ? r.json() : null;
}

async function refresh() {
  const [status, recs] = await Promise.all([
    fetchJSON('/api/status'),
    fetchJSON('/api/records')
  ]);
  if (status) {
    $tgl.className = 'toggle' + (status.enabled ? ' on' : '');
    $tglLabel.textContent = status.enabled ? 'ON' : 'OFF';
    $count.textContent = status.record_count + ' calls';
  }
  if (recs) {
    records = recs;
    renderList();
    renderDetail();
  }
}

function renderList() {
  $list.innerHTML = '';
  if (!records.length) return;
  records.forEach((r, i) => {
    const div = document.createElement('div');
    div.className = 'item' + (i === selected ? ' sel' : '');
    const err = r.error ? ' <span class="err">ERR</span>' : '';
    div.innerHTML =
      `<span class="turn">T${r.turn_index} C#${r.model_call_index}</span>` +
      ` <span class="dur">${(r.duration_ms/1000).toFixed(1)}s</span>` +
      ` <span class="toks">in=${r.usage.input_tokens} out=${r.usage.output_tokens}</span>${err}`;
    div.onclick = () => { selected = i; renderList(); renderDetail(); };
    $list.appendChild(div);
  });
  // auto-select latest
  if (selected < 0 || selected >= records.length) selected = records.length - 1;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function renderDetail() {
  if (!records.length || selected < 0 || selected >= records.length) {
    $detail.innerHTML = '<div id="empty">No records yet.<br><small>Enable Capture and run a turn.</small></div>';
    return;
  }
  const r = records[selected];
  const roleClass = {system:'role-s', human:'role-u', user:'role-u', ai:'role-a', assistant:'role-a', tool:'role-t', developer:'role-s'};
  let html = '';

  // REQUEST
  const reqTok = r.request_messages.reduce((s,m) => s+(m.estimated_tokens||0), 0);
  html += `<div class="sep">${'─'.repeat(58)}</div>`;
  html += `<div class="section">REQUEST  (${r.request_messages.length} msgs, ${reqTok} tok)</div>`;
  html += `<div class="sep">${'─'.repeat(58)}</div>`;
  for (const msg of r.request_messages) {
    const role = msg.role || 'unknown';
    const name = msg.name ? ': ' + msg.name : '';
    const tok = msg.estimated_tokens || 0;
    const cls = roleClass[role] || 'role-t';
    html += `<div class="${cls}">[${role}${name}]  ${tok} tok</div>`;
    const preview = msg.content_preview || '';
    const lines = preview.split('\\n');
    const shown = lines.slice(0, 20);
    html += `<div class="msg-body">${escHtml(shown.join('\\n'))}</div>`;
    if (lines.length > 20) html += `<div class="tc">... (${lines.length} lines total)</div>`;
    if (msg.tool_calls) {
      for (const tc of msg.tool_calls) {
        html += `<div class="tc">└ ${escHtml(tc.name)}(${escHtml((tc.args||'').slice(0,200))})</div>`;
      }
    }
  }

  // RESPONSE
  const respTok = r.response_messages.reduce((s,m) => s+(m.estimated_tokens||0), 0);
  html += `<div class="sep">${'─'.repeat(58)}</div>`;
  html += `<div class="section">RESPONSE  (${r.response_messages.length} msgs, ${respTok} tok)</div>`;
  html += `<div class="sep">${'─'.repeat(58)}</div>`;
  if (r.error) {
    html += `<div style="color:var(--red)">ERROR: ${escHtml(r.error)}</div>`;
  } else if (r.response_messages.length) {
    for (const msg of r.response_messages) {
      const role = msg.role || 'unknown';
      const cls = roleClass[role] || 'role-t';
      html += `<div class="${cls}">[${role}]  ${msg.estimated_tokens||0} tok</div>`;
      const preview = msg.content_preview || '';
      const lines = preview.split('\\n');
      const shown = lines.slice(0, 25);
      html += `<div class="msg-body">${escHtml(shown.join('\\n'))}</div>`;
      if (lines.length > 25) html += `<div class="tc">... (${lines.length} lines total)</div>`;
    }
  } else {
    html += '<div class="meta">(empty response)</div>';
  }

  // META
  html += `<div class="sep">${'─'.repeat(58)}</div>`;
  html += `<div class="section">META</div>`;
  html += `<div class="sep">${'─'.repeat(58)}</div>`;
  html += `<div class="meta">model     ${escHtml(r.model_name)}</div>`;
  html += `<div class="meta">provider  ${escHtml(r.provider)}</div>`;
  html += `<div class="meta">duration  ${r.duration_ms.toFixed(0)}ms</div>`;
  html += `<div class="meta">usage     in=${r.usage.input_tokens} out=${r.usage.output_tokens}</div>`;

  $detail.innerHTML = html;
}

async function toggleCapture() {
  const s = await fetchJSON('/api/toggle', {method:'POST'});
  if (s) {
    $tgl.className = 'toggle' + (s.enabled ? ' on' : '');
    $tglLabel.textContent = s.enabled ? 'ON' : 'OFF';
  }
}

document.addEventListener('keydown', e => {
  if (!records.length) return;
  if (e.key === 'ArrowUp')   { e.preventDefault(); selected = Math.max(0, selected-1); renderList(); renderDetail(); }
  if (e.key === 'ArrowDown') { e.preventDefault(); selected = Math.min(records.length-1, selected+1); renderList(); renderDetail(); }
});

refresh();
pollTimer = setInterval(refresh, 1000);
</script>
</body>
</html>"""

_OLD_PAGE_HTML = r"""\
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Synapse Inspector</title><style>
:root{--bg:#10151d;--panel:#171f2b;--panel2:#202b3a;--line:#34445a;--text:#e7edf5;--muted:#99a9bd;--blue:#64b5f6;--cyan:#55d6c2;--green:#81c995;--amber:#f6c667;--red:#ff8b8b;--violet:#bd9cf6}*{box-sizing:border-box}body{margin:0;height:100vh;overflow:hidden;background:var(--bg);color:var(--text);font:13px/1.45 system-ui,sans-serif}button,input{font:inherit}button{color:inherit;background:transparent;border:0;cursor:pointer}button:focus-visible,input:focus-visible{outline:2px solid var(--blue);outline-offset:2px}#top{height:56px;display:flex;align-items:center;gap:16px;padding:0 18px;background:#131a24;border-bottom:1px solid var(--line)}.brand{font-weight:700;white-space:nowrap}.brand b{color:var(--cyan);font-size:18px}.brand small{display:block;color:var(--muted);font-size:10px}.capture{display:flex;align-items:center;gap:8px;padding-left:16px;border-left:1px solid var(--line);color:var(--muted)}.switch{width:34px;height:20px;padding:2px;border-radius:12px;background:#445267}.switch i{display:block;width:16px;height:16px;border-radius:50%;background:#dce7f5;transition:.15s}.switch.on{background:var(--cyan)}.switch.on i{transform:translateX(14px)}.actions{display:flex;gap:4px;margin-left:auto}.btn{height:30px;padding:0 8px;border:1px solid transparent;border-radius:4px;color:var(--muted)}.btn:hover{background:var(--panel2);border-color:var(--line);color:var(--text)}.danger:hover{color:var(--red)}#app{height:calc(100vh - 56px);display:grid;grid-template-columns:330px minmax(520px,1fr) 245px}#side,#context{background:var(--panel);min-width:0}#side{display:flex;flex-direction:column;border-right:1px solid var(--line)}#context{border-left:1px solid var(--line);overflow:auto}.side-head{padding:14px 12px 10px;border-bottom:1px solid var(--line)}.side-title{display:flex;justify-content:space-between;color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase}.search{height:32px;width:100%;margin-top:10px;padding:0 9px;background:#10161f;color:var(--text);border:1px solid var(--line);border-radius:4px}.filters{display:flex;gap:5px;margin-top:8px}.filter{height:25px;padding:0 7px;border:1px solid var(--line);border-radius:3px;color:var(--muted);font-size:11px}.filter.active,.filter:hover{color:var(--blue);border-color:#43658d;background:#263850}#records{overflow:auto;padding:7px 0}.turn{padding:10px 12px 4px;color:var(--muted);font-size:10px;font-weight:700;text-transform:uppercase}.record{width:100%;padding:10px 12px;border-left:3px solid transparent;text-align:left}.record:hover{background:#1d2734}.record.selected{background:#213044;border-left-color:var(--blue)}.r-top,.r-bottom{display:flex;align-items:center;gap:7px}.r-id{font-weight:650}.r-model{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.r-bottom{margin-top:4px;color:var(--muted);font-size:11px}.dot{width:7px;height:7px;border-radius:50%;background:var(--green);flex:none}.dot.error{background:var(--red)}.slow{color:var(--amber)}.err{color:var(--red)}#detail{overflow:auto;min-width:0;padding:20px 24px 48px;background:#121923}#empty{height:100%;display:grid;place-content:center;text-align:center;gap:8px;color:var(--muted)}#empty strong{font-size:16px;color:var(--text)}.head{display:flex;justify-content:space-between;gap:20px;margin-bottom:18px}.eyebrow{color:var(--cyan);font-size:11px;font-weight:700;text-transform:uppercase}h1{margin:3px 0;font-size:20px}.sub{color:var(--muted);font-size:12px}.summary{display:grid;grid-template-columns:repeat(4,minmax(100px,1fr));margin-bottom:16px;border:1px solid var(--line);border-radius:5px;overflow:hidden}.metric{padding:11px 12px;border-right:1px solid var(--line)}.metric:last-child{border:0}.metric label{display:block;color:var(--muted);font-size:10px;font-weight:700;text-transform:uppercase}.metric strong{display:block;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}.section{margin-top:10px;border:1px solid var(--line);border-radius:5px;background:var(--panel)}details[open]{background:#18212e}summary{display:flex;align-items:center;min-height:42px;padding:0 13px;cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}summary:before{content:'>';width:16px;color:var(--muted);font-family:monospace}details[open]>summary:before{transform:rotate(90deg);color:var(--blue)}.sname{font-weight:650}.smeta{margin-left:8px;color:var(--muted);font-size:11px}.copy{margin-left:auto}.body{padding:12px;border-top:1px solid var(--line)}.msg{margin:8px 0;border-left:3px solid var(--line);background:#121923}.msg.system,.msg.developer{border-color:var(--violet)}.msg.user,.msg.human{border-color:var(--blue)}.msg.assistant,.msg.ai{border-color:var(--green)}.msg.tool{border-color:var(--amber)}.mhead{display:flex;align-items:center;gap:8px;padding:7px 9px;border-bottom:1px solid #263245}.role{font-size:11px;font-weight:700;text-transform:uppercase}.system .role,.developer .role{color:var(--violet)}.user .role,.human .role{color:var(--blue)}.assistant .role,.ai .role{color:var(--green)}.tool .role{color:var(--amber)}.meta{color:var(--muted);font-size:11px}.mhead .btn{margin-left:auto}.content,pre{margin:0;max-height:280px;overflow:auto;padding:9px;white-space:pre-wrap;overflow-wrap:anywhere;color:#d9e3ee;font:12px/1.55 ui-monospace,Consolas,monospace}.call{margin:8px;padding:9px;border:1px solid #3b526d;border-radius:4px;background:#142231}.call b{color:var(--cyan)}.error-box{padding:10px;color:#ffd1d1;background:#351e29;border-left:3px solid var(--red);white-space:pre-wrap;overflow-wrap:anywhere;font-family:monospace}.ctitle{padding:15px 14px 10px;color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase}.cblock{padding:0 14px 14px;border-bottom:1px solid var(--line)}.kv{display:grid;grid-template-columns:80px minmax(0,1fr);gap:7px;padding:5px 0;font-size:12px}.kv span:first-child{color:var(--muted)}.kv span:last-child{overflow-wrap:anywhere}@media(max-width:1100px){#app{grid-template-columns:290px minmax(520px,1fr)}#context{display:none}}@media(max-width:760px){#app{display:block}#side{display:none}.summary{grid-template-columns:repeat(2,1fr)}.metric:nth-child(2){border-right:0}}
</style></head><body><header id="top"><div class="brand"><b>//</b> Synapse Inspector<small>AGENT COMMUNICATION ANALYSIS</small></div><button class="capture" id="capture" title="启用或暂停通信采集">采集 <span class="switch"><i></i></span><strong id="state">关闭</strong></button><div class="actions"><button class="btn" id="refresh">刷新</button><label class="btn"><input id="auto" type="checkbox" checked> 自动</label><button class="btn danger" id="clear">清空</button></div></header><main id="app"><aside id="side"><div class="side-head"><div class="side-title"><span>调用记录</span><span id="count">0 条</span></div><input class="search" id="search" type="search" placeholder="筛选模型、回合或错误"><div class="filters"><button class="filter active" data-f="all">全部</button><button class="filter" data-f="error">异常</button><button class="filter" data-f="tool">工具</button><button class="filter" data-f="slow">慢调用</button></div></div><div id="records"></div></aside><section id="detail"><div id="empty"><strong>等待通信记录</strong><span>开启采集后，模型调用会在这里按请求和响应链路展示。</span></div></section><aside id="context"><div class="ctitle">分析上下文</div><div class="cblock" id="ctx"><div class="kv"><span>状态</span><span>尚未选择记录</span></div></div><div class="ctitle">记录范围</div><div class="cblock"><div class="kv"><span>当前采集</span><span>最终 Provider-ready LLM 请求 / 响应</span></div><div class="kv"><span>工具消息</span><span>作为请求或响应消息的独立类型展示</span></div></div></aside></main><script>
let records=[],selected=-1,filter='all',timer;const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),dump=v=>JSON.stringify(v,null,2),ms=v=>v>=1000?(v/1000).toFixed(2)+' s':Math.round(v||0)+' ms';async function api(u,o){try{let r=await fetch(u,o);return r.ok?r.json():null}catch(_){return null}}function cp(s){navigator.clipboard?.writeText(s).catch(()=>{})}function shown(){let q=$('search').value.trim().toLowerCase();return records.map((r,i)=>({...r,i})).filter(r=>{let tool=[...r.request_messages,...r.response_messages].some(m=>m.tool_calls?.length||m.role==='tool'),ok=filter==='all'||filter==='error'&&r.error||filter==='tool'&&tool||filter==='slow'&&r.duration_ms>=1000;return ok&&(!q||`${r.turn_index} ${r.model_call_index} ${r.model_name} ${r.provider} ${r.error||''}`.toLowerCase().includes(q))})}function drawList(){let out=$('records'),last;out.innerHTML='';let rows=shown();$('count').textContent=`${rows.length} / ${records.length} 条`;rows.forEach(r=>{if(last!==r.turn_index){last=r.turn_index;out.insertAdjacentHTML('beforeend',`<div class="turn">回合 ${r.turn_index}</div>`)}let tool=[...r.request_messages,...r.response_messages].some(m=>m.tool_calls?.length||m.role==='tool'),b=document.createElement('button');b.className='record'+(r.i===selected?' selected':'');b.innerHTML=`<div class="r-top"><span class="dot ${r.error?'error':''}"></span><span class="r-id">调用 #${r.model_call_index}</span><span class="r-model">${esc(r.model_name)}</span></div><div class="r-bottom"><span class="${r.duration_ms>=1000?'slow':''}">${ms(r.duration_ms)}</span><span>${r.usage?.input_tokens||0} in / ${r.usage?.output_tokens||0} out</span>${tool?'<span>tool</span>':''}${r.error?'<span class="err">失败</span>':''}</div>`;b.onclick=()=>{selected=r.i;drawList();drawDetail()};out.appendChild(b)})}function sec(n,m,b,open=true,raw=''){return `<details class="section" ${open?'open':''}><summary><span class="sname">${n}</span><span class="smeta">${m}</span>${raw?'<button class="btn copy" data-raw="'+encodeURIComponent(raw)+'">复制</button>':''}</summary><div class="body">${b}</div></details>`}function msg(m){let role=(m.role||'unknown').toLowerCase(),text=m.content_full||m.content_preview||'',calls=(m.tool_calls||[]).map(c=>`<div class="call"><b>工具调用 · ${esc(c.name||'unknown')}</b><pre>${esc(c.args||'{}')}</pre></div>`).join('');return `<article class="msg ${role}"><div class="mhead"><span class="role">${esc(role)}</span><span class="meta">${m.name?' / '+esc(m.name):''} ${m.estimated_tokens||0} tokens${m.tool_call_id?' · '+esc(m.tool_call_id):''}</span><button class="btn cm" data-t="${encodeURIComponent(text)}">复制</button></div><div class="content">${esc(text)}</div>${calls}</article>`}function drawDetail(){let r=records[selected];if(!r){$('detail').innerHTML='<div id="empty"><strong>等待通信记录</strong><span>开启采集后，模型调用会在这里按请求和响应链路展示。</span></div>';$('ctx').innerHTML='<div class="kv"><span>状态</span><span>尚未选择记录</span></div>';return}let req=r.request_messages||[],resp=r.response_messages||[],rt=req.reduce((n,m)=>n+(m.estimated_tokens||0),0),tools=[...req,...resp].flatMap(m=>m.tool_calls||[]),status=r.error?'失败':'已完成';$('detail').innerHTML=`<div class="head"><div><div class="eyebrow">模型通信 / 回合 ${r.turn_index}</div><h1>调用 #${r.model_call_index} <span class="${r.error?'err':''}">${status}</span></h1><div class="sub">${esc(r.provider)} · ${esc(r.model_name)} · ${new Date((r.started_at||0)*1000).toLocaleString()}</div></div><div><button class="btn" id="expand">展开</button><button class="btn" id="collapse">收起</button><button class="btn" id="copyall">复制 JSON</button></div></div><div class="summary"><div class="metric"><label>状态</label><strong class="${r.error?'err':''}">${status}</strong></div><div class="metric"><label>耗时</label><strong>${ms(r.duration_ms)}</strong></div><div class="metric"><label>输入 / 输出</label><strong>${r.usage?.input_tokens||0} / ${r.usage?.output_tokens||0}</strong></div><div class="metric"><label>通信消息</label><strong>${req.length} 请求 / ${resp.length} 响应</strong></div></div>${sec('请求消息',`${req.length} 条 · 估算 ${rt} tokens`,req.map(msg).join('')||'<span class="sub">请求没有可展示的消息。</span>',true,dump(req))}${sec('响应消息',`${resp.length} 条 · ${r.usage?.output_tokens||0} output tokens`,resp.map(msg).join('')||'<span class="sub">模型没有返回消息。</span>',true,dump(resp))}${tools.length?sec('工具调用',`${tools.length} 项`,tools.map(c=>`<div class="call"><b>${esc(c.name||'unknown')}</b><pre>${esc(c.args||'{}')}</pre></div>`).join(''),true,dump(tools)):''}${r.error?sec('异常诊断','调用失败',`<div class="error-box">${esc(r.error)}</div>`,true,r.error):''}${sec('原始记录','结构化快照',`<pre>${esc(dump(r))}</pre>`,false,dump(r))}`;$('ctx').innerHTML=`<div class="kv"><span>Provider</span><span>${esc(r.provider)}</span></div><div class="kv"><span>Model</span><span>${esc(r.model_name)}</span></div><div class="kv"><span>回合 / 调用</span><span>${r.turn_index} / ${r.model_call_index}</span></div><div class="kv"><span>结果</span><span class="${r.error?'err':''}">${status}</span></div>`;$('copyall').onclick=()=>cp(dump(r));$('expand').onclick=()=>$('detail').querySelectorAll('details').forEach(x=>x.open=true);$('collapse').onclick=()=>$('detail').querySelectorAll('details').forEach(x=>x.open=false);$('detail').querySelectorAll('.cm').forEach(b=>b.onclick=()=>cp(decodeURIComponent(b.dataset.t)));$('detail').querySelectorAll('.copy').forEach(b=>b.onclick=e=>{e.preventDefault();cp(decodeURIComponent(b.dataset.raw))})}async function refresh(){let[s,recs]=await Promise.all([api('/api/status'),api('/api/records')]);if(s){$('capture').querySelector('.switch').classList.toggle('on',!!s.enabled);$('state').textContent=s.enabled?'开启':'关闭'}if(recs){let n=records.length;records=recs;if(selected<0&&records.length)selected=records.length-1;if(selected>=records.length)selected=records.length-1;drawList();if(records.length!==n||document.activeElement!==$('search'))drawDetail()}}$('capture').onclick=async()=>{await api('/api/toggle',{method:'POST'});refresh()};$('refresh').onclick=refresh;$('clear').onclick=async()=>{if(records.length&&confirm('清空全部已采集的通信记录？')){await api('/api/clear',{method:'POST'});selected=-1;refresh()}};$('search').oninput=drawList;document.querySelectorAll('.filter').forEach(b=>b.onclick=()=>{filter=b.dataset.f;document.querySelectorAll('.filter').forEach(x=>x.classList.toggle('active',x===b));drawList()});$('auto').onchange=e=>{clearInterval(timer);if(e.target.checked)timer=setInterval(refresh,1000)};document.addEventListener('keydown',e=>{if(['ArrowUp','ArrowDown'].includes(e.key)&&records.length&&document.activeElement!==$('search')){e.preventDefault();selected=Math.max(0,Math.min(records.length-1,selected+(e.key==='ArrowUp'?-1:1)));drawList();drawDetail()}});refresh();timer=setInterval(refresh,1000);
</script></body></html>"""

# ---------------------------------------------------------------------------
# Polling transfers only lightweight record summaries. Full request and response
# bodies are requested once after an operator selects a record.
_PAGE_HTML = r"""\
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Synapse Inspector</title><style>
:root{--bg:#0e141c;--panel:#151e29;--raised:#1d2937;--line:#344456;--text:#e5edf5;--muted:#9aaabd;--blue:#69b7ff;--cyan:#55d5c0;--green:#81c995;--amber:#f5c66c;--red:#ff9494;--violet:#c0a5f6}*{box-sizing:border-box}body{margin:0;height:100vh;overflow:hidden;background:var(--bg);color:var(--text);font:13px/1.45 system-ui,sans-serif}button,input{font:inherit}button{border:0;color:inherit;background:transparent;cursor:pointer}button:focus-visible,input:focus-visible{outline:2px solid var(--blue);outline-offset:2px}#top{height:52px;display:flex;align-items:center;gap:18px;padding:0 16px;border-bottom:1px solid var(--line);background:#121a24}.brand{font-weight:700;white-space:nowrap}.brand b{color:var(--cyan);font-size:18px}.brand small{display:block;color:var(--muted);font-size:9px;font-weight:600}.capture{display:flex;align-items:center;gap:8px;padding-left:16px;border-left:1px solid var(--line);color:var(--muted)}.switch{width:32px;height:18px;padding:2px;border-radius:9px;background:#455367}.switch i{display:block;width:14px;height:14px;border-radius:50%;background:#e2ebf4;transition:transform .15s}.switch.on{background:var(--cyan)}.switch.on i{transform:translateX(14px)}.actions{display:flex;gap:4px;margin-left:auto}.btn{height:28px;padding:0 8px;border:1px solid transparent;border-radius:4px;color:var(--muted)}.btn:hover{background:var(--raised);border-color:var(--line);color:var(--text)}.danger:hover,.err{color:var(--red)}#app{height:calc(100vh - 52px);display:grid;grid-template-columns:320px minmax(480px,1fr) 230px}#side,#context{min-width:0;background:var(--panel)}#side{display:flex;flex-direction:column;border-right:1px solid var(--line)}#context{overflow:auto;border-left:1px solid var(--line)}.side-head{padding:12px;border-bottom:1px solid var(--line)}.side-title{display:flex;justify-content:space-between;color:var(--muted);font-size:10px;font-weight:700;text-transform:uppercase}.search{width:100%;height:31px;margin-top:9px;padding:0 9px;border:1px solid var(--line);border-radius:4px;background:#0f161f;color:var(--text)}.filters{display:flex;gap:4px;margin-top:7px}.filter{height:24px;padding:0 6px;border:1px solid var(--line);border-radius:3px;color:var(--muted);font-size:11px}.filter.active,.filter:hover{border-color:#426991;background:#1f3247;color:var(--blue)}#records{overflow:auto;padding:7px 0}.turn{padding:8px 12px 3px;color:var(--muted);font-size:10px;font-weight:700;text-transform:uppercase}.record{display:block;width:100%;padding:9px 12px;border-left:3px solid transparent;text-align:left}.record:hover{background:#1b2734}.record.selected{border-left-color:var(--blue);background:#213247}.r-top,.r-bottom{display:flex;align-items:center;gap:7px}.r-id{font-weight:650}.r-model{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}.r-bottom{margin-top:3px;color:var(--muted);font-size:11px}.dot{width:7px;height:7px;flex:none;border-radius:50%;background:var(--green)}.dot.error{background:var(--red)}.slow{color:var(--amber)}#detail{overflow:auto;min-width:0;padding:20px 24px 48px;background:#101720}#empty{display:grid;height:100%;place-content:center;gap:8px;text-align:center;color:var(--muted)}#empty strong{color:var(--text);font-size:16px}.head{display:flex;justify-content:space-between;gap:16px;margin-bottom:16px}.eyebrow,.ctitle{color:var(--cyan);font-size:10px;font-weight:700;text-transform:uppercase}h1{margin:3px 0;font-size:20px}.sub,.meta{color:var(--muted);font-size:12px}.summary{display:grid;grid-template-columns:repeat(4,minmax(90px,1fr));margin-bottom:14px;border:1px solid var(--line);border-radius:5px;overflow:hidden}.metric{padding:10px 11px;border-right:1px solid var(--line)}.metric:last-child{border:0}.metric label{display:block;color:var(--muted);font-size:10px;font-weight:700;text-transform:uppercase}.metric strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}.section{margin-top:10px;border:1px solid var(--line);border-radius:5px;background:var(--panel)}details[open]{background:#182331}summary{display:flex;align-items:center;min-height:40px;padding:0 12px;cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}summary:before{content:'>';width:16px;color:var(--muted);font-family:monospace}details[open]>summary:before{color:var(--blue);transform:rotate(90deg)}.sname{font-weight:650}.smeta{margin-left:8px;color:var(--muted);font-size:11px}.copy{margin-left:auto}.body{padding:10px;border-top:1px solid var(--line)}.msg{margin:7px 0;border-left:3px solid var(--line);background:#101720}.msg.system,.msg.developer{border-color:var(--violet)}.msg.user,.msg.human{border-color:var(--blue)}.msg.assistant,.msg.ai{border-color:var(--green)}.msg.tool{border-color:var(--amber)}.mhead{display:flex;align-items:center;gap:8px;padding:7px 9px;border-bottom:1px solid #273446}.role{font-size:11px;font-weight:700;text-transform:uppercase}.mhead .btn{margin-left:auto}.content,pre{max-height:300px;margin:0;overflow:auto;padding:9px;white-space:pre-wrap;overflow-wrap:anywhere;color:#d9e4ee;font:12px/1.55 ui-monospace,Consolas,monospace}.more{margin:0 9px 9px;color:var(--blue);font-size:12px}.call{margin:8px;padding:8px;border:1px solid #3a536d;border-radius:4px;background:#142231}.tool-pair{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px;margin:8px 0}.tool-side{min-width:0;border:1px solid #3a536d;border-radius:4px;background:#101720}.tool-label{padding:7px 9px;border-bottom:1px solid #273446;color:var(--cyan);font-size:11px;font-weight:700}.tool-side.result .tool-label{color:var(--green)}.tool-side.missing .tool-label{color:var(--amber)}.tool-side pre{max-height:240px}@media(max-width:760px){.tool-pair{grid-template-columns:1fr}}.call b{color:var(--cyan)}.error-box{padding:10px;border-left:3px solid var(--red);background:#351e29;color:#ffd1d1;white-space:pre-wrap;overflow-wrap:anywhere;font-family:monospace}.ctitle{padding:15px 14px 9px;color:var(--muted)}.cblock{padding:0 14px 14px;border-bottom:1px solid var(--line)}.kv{display:grid;grid-template-columns:75px minmax(0,1fr);gap:6px;padding:5px 0;font-size:12px}.kv span:first-child{color:var(--muted)}.kv span:last-child{overflow-wrap:anywhere}@media(max-width:1100px){#app{grid-template-columns:290px minmax(480px,1fr)}#context{display:none}}@media(max-width:760px){#app{display:block}#side{display:none}.summary{grid-template-columns:repeat(2,1fr)}.metric:nth-child(2){border-right:0}}
</style></head><body><header id="top"><div class="brand"><b>//</b> Synapse Inspector<small>AGENT COMMUNICATION ANALYSIS</small></div><button class="capture" id="capture">采集 <span class="switch"><i></i></span><strong id="state">关闭</strong></button><div class="actions"><button class="btn" id="refresh">刷新</button><label class="btn"><input id="auto" type="checkbox" checked> 自动</label><button class="btn danger" id="clear">清空</button></div></header><main id="app"><aside id="side"><div class="side-head"><div class="side-title"><span>调用记录</span><span id="count">0 条</span></div><input class="search" id="search" type="search" placeholder="筛选模型、回合或错误"><div class="filters"><button class="filter active" data-f="all">全部</button><button class="filter" data-f="error">异常</button><button class="filter" data-f="tool">工具</button><button class="filter" data-f="slow">慢调用</button></div></div><div id="records"></div></aside><section id="detail"><div id="empty"><strong>等待通信记录</strong><span>开启采集后，模型调用会在这里按请求和响应链路展示。</span></div></section><aside id="context"><div class="ctitle">分析上下文</div><div class="cblock" id="ctx"><div class="kv"><span>状态</span><span>尚未选择记录</span></div></div></aside></main><script>
let records=[],selected=-1,filter='all',timer,loaded=-1;const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),dump=v=>JSON.stringify(v,null,2),ms=v=>v>=1000?(v/1000).toFixed(2)+' s':Math.round(v||0)+' ms';async function api(u,o){try{const r=await fetch(u,o);return r.ok?r.json():null}catch(_){return null}}function cp(s){navigator.clipboard?.writeText(s).catch(()=>{})}function rows(){const q=$('search').value.trim().toLowerCase();return records.filter(r=>(filter==='all'||filter==='error'&&r.error||filter==='tool'&&r.has_tools||filter==='slow'&&r.duration_ms>=1000)&&(!q||`${r.turn_index} ${r.model_call_index} ${r.model_name} ${r.provider} ${r.error||''}`.toLowerCase().includes(q)))}function drawList(){const out=$('records'),visible=rows();let last;out.innerHTML='';$('count').textContent=`${visible.length} / ${records.length} 条`;for(const r of visible){if(last!==r.turn_index){last=r.turn_index;out.insertAdjacentHTML('beforeend',`<div class="turn">回合 ${r.turn_index}</div>`)}const b=document.createElement('button');b.className='record'+(r.index===selected?' selected':'');b.innerHTML=`<div class="r-top"><span class="dot ${r.error?'error':''}"></span><span class="r-id">调用 #${r.model_call_index}</span><span class="r-model">${esc(r.model_name)}</span></div><div class="r-bottom"><span class="${r.duration_ms>=1000?'slow':''}">${ms(r.duration_ms)}</span><span>${r.usage?.input_tokens||0} in / ${r.usage?.output_tokens||0} out</span>${r.has_tools?'<span>tool</span>':''}${r.error?'<span class="err">失败</span>':''}</div>`;b.onclick=()=>select(r.index);out.appendChild(b)}}function section(name,meta,body,open=true,raw=''){return `<details class="section" ${open?'open':''}><summary><span class="sname">${name}</span><span class="smeta">${meta}</span>${raw?'<button class="btn copy" data-raw="'+encodeURIComponent(raw)+'">复制</button>':''}</summary><div class="body">${body}</div></details>`}function message(m){const role=(m.role||'unknown').toLowerCase(),text=m.content_full||m.content_preview||'',limit=4000,shown=text.slice(0,limit),calls=(m.tool_calls||[]).map(c=>`<div class="call"><b>工具调用 · ${esc(c.name||'unknown')}</b><pre>${esc(c.args||'{}')}</pre></div>`).join(''),more=text.length>limit?`<button class="more" data-more="${encodeURIComponent(text)}">展开剩余 ${text.length-limit} 字符</button>`:'';return `<article class="msg ${role}"><div class="mhead"><span class="role">${esc(role)}</span><span class="meta">${m.name?' / '+esc(m.name):''} ${m.estimated_tokens||0} tokens</span><button class="btn cm" data-t="${encodeURIComponent(text)}">复制</button></div><div class="content">${esc(shown)}</div>${more}${calls}</article>`}function toolPairs(pairs){return pairs.map(pair=>{const result=pair.result==null?'尚未捕获对应工具响应':pair.result;return `<div class="tool-pair"><div class="tool-side"><div class="tool-label">工具调用 · ${esc(pair.name)}</div><pre>${esc(pair.args||'{}')}</pre></div><div class="tool-side ${pair.result==null?'missing':'result'}"><div class="tool-label">工具响应${pair.result==null?' · 缺失':''}</div><pre>${esc(result)}</pre></div></div>`}).join('')}function drawDetail(r){if(!r){$('detail').innerHTML='<div id="empty"><strong>等待通信记录</strong><span>开启采集后，模型调用会在这里按请求和响应链路展示。</span></div>';return}const req=r.request_messages||[],resp=r.response_messages||[],pairs=r.tool_pairs||[],deltaStart=r.request_delta_start||0,delta=req.slice(deltaStart),status=r.error?'失败':'已完成',tokens=delta.reduce((n,m)=>n+(m.estimated_tokens||0),0);$('detail').innerHTML=`<div class="head"><div><div class="eyebrow">模型通信 / 回合 ${r.turn_index}</div><h1>调用 #${r.model_call_index} <span class="${r.error?'err':''}">${status}</span></h1><div class="sub">${esc(r.provider)} · ${esc(r.model_name)} · ${new Date((r.started_at||0)*1000).toLocaleString()}</div></div><div><button class="btn" id="expand">展开</button><button class="btn" id="collapse">收起</button><button class="btn" id="copyall">复制原始记录</button></div></div><div class="summary"><div class="metric"><label>状态</label><strong class="${r.error?'err':''}">${status}</strong></div><div class="metric"><label>耗时</label><strong>${ms(r.duration_ms)}</strong></div><div class="metric"><label>输入 / 输出</label><strong>${r.usage?.input_tokens||0} / ${r.usage?.output_tokens||0}</strong></div><div class="metric"><label>通信消息</label><strong>${req.length} 请求 / ${resp.length} 响应</strong></div></div>${section('请求增量',`${delta.length} 新增 / ${req.length} 总消息 · 估算 ${tokens} tokens`,delta.map(message).join('')||'<span class="sub">本次调用未新增请求消息，沿用同回合上下文。</span>',true,dump(delta))}${section('响应消息',`${resp.length} 条 · ${r.usage?.output_tokens||0} output tokens`,resp.map(message).join('')||'<span class="sub">模型没有返回消息。</span>',true,dump(resp))}${pairs.length?section('工具调用',`${pairs.length} 组 · 当前回合关联`,toolPairs(pairs),true,dump(pairs)):''}${r.error?section('异常诊断','调用失败',`<div class="error-box">${esc(r.error)}</div>`,true,r.error):''}<details class="section" id="raw"><summary><span class="sname">原始记录</span><span class="smeta">按需加载</span></summary><div class="body"><span class="sub">展开后加载完整捕获快照。</span></div></details>`;$('ctx').innerHTML=`<div class="kv"><span>Provider</span><span>${esc(r.provider)}</span></div><div class="kv"><span>Model</span><span>${esc(r.model_name)}</span></div><div class="kv"><span>回合 / 调用</span><span>${r.turn_index} / ${r.model_call_index}</span></div><div class="kv"><span>结果</span><span class="${r.error?'err':''}">${status}</span></div>`;const loadRaw=async copy=>{const raw=await api('/api/records/'+selected+'/raw');if(!raw||selected!==r._index)return;const text=dump(raw);if(copy){cp(text);return}const rawBox=$('raw');if(rawBox){rawBox.innerHTML=`<summary><span class="sname">原始记录</span><span class="smeta">完整捕获快照</span><button class="btn copy" data-raw="${encodeURIComponent(text)}">复制</button></summary><div class="body"><pre>${esc(text)}</pre></div>`;rawBox.querySelector('.copy').onclick=e=>{e.preventDefault();cp(text)}}};r._index=selected;$('copyall').onclick=()=>loadRaw(true);$('raw').ontoggle=e=>{if(e.target.open&&!e.target.dataset.loaded){e.target.dataset.loaded='1';loadRaw(false)}};$('expand').onclick=()=>$('detail').querySelectorAll('details').forEach(x=>x.open=true);$('collapse').onclick=()=>$('detail').querySelectorAll('details').forEach(x=>x.open=false);$('detail').querySelectorAll('.cm').forEach(b=>b.onclick=()=>cp(decodeURIComponent(b.dataset.t)));$('detail').querySelectorAll('.more').forEach(b=>b.onclick=()=>{b.previousElementSibling.textContent=decodeURIComponent(b.dataset.more);b.remove()});$('detail').querySelectorAll('.copy').forEach(b=>b.onclick=e=>{e.preventDefault();cp(decodeURIComponent(b.dataset.raw))})}async function select(index){selected=index;drawList();if(loaded===index)return;loaded=-1;$('detail').innerHTML='<div id="empty"><strong>正在加载调用详情</strong></div>';const record=await api('/api/records/'+index);if(selected===index&&record){loaded=index;drawDetail(record)}}async function refresh(){const [status,next]=await Promise.all([api('/api/status'),api('/api/records')]);if(status){$('capture').querySelector('.switch').classList.toggle('on',!!status.enabled);$('state').textContent=status.enabled?'开启':'关闭'}if(next){const changed=next.length!==records.length||next.some((r,i)=>r.index!==records[i]?.index||r.duration_ms!==records[i]?.duration_ms);records=next;if(selected<0&&records.length)select(records.at(-1).index);else if(selected>=records.length){selected=-1;loaded=-1;drawDetail(null)}if(changed)drawList()}}$('capture').onclick=async()=>{await api('/api/toggle',{method:'POST'});refresh()};$('refresh').onclick=refresh;$('clear').onclick=async()=>{if(records.length&&confirm('清空全部已采集的通信记录？')){await api('/api/clear',{method:'POST'});records=[];selected=-1;loaded=-1;drawList();drawDetail(null)}};$('search').oninput=drawList;document.querySelectorAll('.filter').forEach(b=>b.onclick=()=>{filter=b.dataset.f;document.querySelectorAll('.filter').forEach(x=>x.classList.toggle('active',x===b));drawList()});$('auto').onchange=e=>{clearInterval(timer);if(e.target.checked)timer=setInterval(refresh,1000)};refresh();timer=setInterval(refresh,1000);
</script></body></html>"""

# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------


class _DebugHandler(BaseHTTPRequestHandler):
    """Serves the inspector page and JSON API endpoints."""

    # Class-level reference set by DebugHttpServer before starting.
    store: DebugCaptureStore | None = None

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default HTTP log noise."""
        pass

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content: str, status: int = 200) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, content: str, status: int = 200) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._html(_PAGE_HTML)
            return

        if path == "/api/status":
            store = self.store or getattr(self.__class__, "store", None)
            if store is None:
                self._json({"error": "no store"}, 500)
                return
            self._json({"enabled": store.enabled, "record_count": store.record_count})
            return

        if path == "/api/records":
            store = self.store or getattr(self.__class__, "store", None)
            if store is None:
                self._json({"error": "no store"}, 500)
                return
            records = store.records()
            self._json([_record_summary(record, index) for index, record in enumerate(records)])
            return

        if path.startswith("/api/records/"):
            store = self.store or getattr(self.__class__, "store", None)
            if store is None:
                self._json({"error": "no store"}, 500)
                return
            raw = path.endswith("/raw")
            suffix = "/raw" if raw else ""
            try:
                index = int(path.removeprefix("/api/records/").removesuffix(suffix))
            except ValueError:
                self._text("Not Found", 404)
                return
            records = store.records()
            if not 0 <= index < len(records):
                self._text("Not Found", 404)
                return
            if raw:
                self._json(_record_to_raw_dict(records[index]))
            else:
                detail = _record_to_dict(records[index], request_delta_start=_request_delta_start(records, index))
                detail["tool_pairs"] = _tool_pairs(records, index)
                self._json(detail)
            return

        self._text("Not Found", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/toggle":
            store = self.store or getattr(self.__class__, "store", None)
            if store is None:
                self._json({"error": "no store"}, 500)
                return
            store.enabled = not store.enabled
            self._json({"enabled": store.enabled, "record_count": store.record_count})
            return

        if path == "/api/clear":
            store = self.store or getattr(self.__class__, "store", None)
            if store is None:
                self._json({"error": "no store"}, 500)
                return
            store.clear()
            self._json({"enabled": store.enabled, "record_count": store.record_count})
            return

        self._text("Not Found", 404)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


class DebugHttpServer:
    """Manages the background HTTP debug inspector server.

    Usage::

        server = DebugHttpServer(get_debug_store())
        server.start()       # background thread
        server.open_browser()  # opens in default browser
        # ...
        server.stop()
    """

    def __init__(
        self,
        store: DebugCaptureStore,
        *,
        host: str = _DEFAULT_HOST,
    ) -> None:
        self._store = store
        self._host = host
        self._port: int = 0  # assigned on start()
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the HTTP server in a daemon background thread."""
        if self._httpd is not None:
            return  # already running

        self._port = _find_free_port()
        _DebugHandler.store = self._store
        self._httpd = HTTPServer((self._host, self._port), _DebugHandler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="debug-http-server",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut down the HTTP server."""
        httpd = self._httpd
        if httpd is not None:
            httpd.shutdown()
            self._httpd = None
        self._thread = None

    def open_browser(self) -> None:
        """Open the inspector page in the default web browser."""
        webbrowser.open(self.url)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> DebugHttpServer:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Singleton convenience
# ---------------------------------------------------------------------------

_server: DebugHttpServer | None = None


def get_debug_server() -> DebugHttpServer:
    """Return a process-level DebugHttpServer singleton (does NOT auto-start)."""
    global _server
    from synapse.observability.llm_debug import get_debug_store

    if _server is None:
        _server = DebugHttpServer(get_debug_store())
    return _server
