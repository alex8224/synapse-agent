"""Opt-in acceptance with a new daemon and the configured default model.

Run: uv run --no-sync python scripts/verify_session_collaboration.py

Uses two read-only fixture projects and a private catalog below .tmp/. Never
selects an existing project, replaces the user's daemon, or prints credentials.
Success requires the source to consume the reply, not merely a 'replied' request.
The process is restarted afterwards to verify durable history and no replay.

Three harness modes exist so the browser acceptance and the pytest regression
share exactly one fixture definition and one daemon entry:

* ``--prepare ROOT`` writes the fixture projects and prints their marker;
* ``--serve ROOT`` runs an isolated daemon (``--scripted`` swaps only the chat
  model for a deterministic one, ``--gate FILE`` holds the peer's reply until
  that file appears, ``--fixtures FILE`` registers both projects and creates
  their sessions before announcing readiness);
* the default mode runs the whole acceptance against the default model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"turn_completed", "turn_cancelled", "turn_failed", "turn_waiting_approval"}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class Client:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.sequence = 0
        self.labels: dict[str, str] = {}
        self.completed: dict[str, int] = {}
        self.tool_calls: dict[str, int] = {}
        self.failure: str | None = None
        self.phases: dict[str, str] = {}

    def observe(self, frame: dict[str, Any]) -> None:
        params = frame.get("params", {})
        event = params.get("event", {})
        kind, payload = event.get("kind"), event.get("payload", {})
        label = self.labels.get(params.get("subscription_id", ""), "session")
        if kind == "activity_updated":
            phase = str(payload.get("detail", ""))[:100]
            if phase != self.phases.get(label):
                self.phases[label] = phase
                log(f"{label}: {phase}")
        elif kind == "tool_started":
            self.tool_calls[label] = self.tool_calls.get(label, 0) + 1
            log(f"{label}: tool={payload.get('name', '')}")
        elif kind in TERMINAL:
            log(f"{label}: {kind}")
            if kind == "turn_completed":
                self.completed[label] = self.completed.get(label, 0) + 1
            else:
                self.failure = f"{label}: {kind}"

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.sequence += 1
        request_id = self.sequence
        await self.ws.send(json.dumps({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }))
        async with asyncio.timeout(45):
            while True:
                frame = json.loads(await self.ws.recv())
                if frame.get("id") != request_id:
                    self.observe(frame)
                    continue
                if "error" in frame:
                    code = frame["error"].get("data", {}).get("service_code")
                    raise RuntimeError(f"{method}: {code or frame['error']['code']}")
                return frame["result"]

    async def watch(self, label: str, session: dict[str, str]) -> None:
        result = await self.call("runtime.events.watch", {
            "session": session, "after": 0, "queue_size": 1024,
        })
        self.labels[result["subscription_id"]] = label


def scripted_model(gate: Path | None) -> Any:
    """A chat model that picks real tools, so no collaboration step is faked.

    Only the model's *choice* is scripted: the tools, middleware, coordinator,
    delivery runtime, WebSocket transport and SQLite mailbox are all production
    code.  ``gate`` holds the peer's final reply until the file appears, which is
    what lets a browser run observe the waiting state deterministically instead
    of racing a fast turn.
    """
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class ScriptedModel(FakeMessagesListChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # noqa: ARG002
            return self

        def _generate(
            self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any,
        ) -> Any:  # noqa: ARG002
            index = max(
                i for i, message in enumerate(messages) if isinstance(message, HumanMessage)
            )
            text = str(messages[index].content)
            results = [m for m in messages[index + 1:] if isinstance(m, ToolMessage)]
            if "你的角色：被请求方" in text:
                if not results:
                    reply = self._tool("read_file", {"file_path": "/FACT.txt"})
                elif results[-1].name == "read_file":
                    marker = re.search(r"COLLABORATION_OK_[0-9a-f]+", str(results[-1].content))
                    if marker is None:
                        return ChatResult(generations=[ChatGeneration(
                            message=AIMessage(content="The fixture marker is missing."))])
                    if gate is not None:
                        deadline = time.monotonic() + 90
                        while not gate.exists() and time.monotonic() < deadline:
                            time.sleep(0.1)
                    reply = self._tool("reply_to_collaboration", {
                        "kind": "reply", "message": marker[0],
                    })
                else:
                    reply = AIMessage(content="Delivered the fixture marker.")
            elif "你的角色：发起方" in text:
                marker = re.search(r"COLLABORATION_OK_[0-9a-f]+", text)
                reply = AIMessage(content=(
                    f"The peer confirmed: {marker[0]}" if marker is not None
                    else "The peer answered without the marker."
                ))
            elif results:
                reply = AIMessage(content="Request sent; waiting for the peer's reply.")
            else:
                target = re.search(r"@session:([0-9a-f]+:[0-9a-f]+)", text)
                if target is None:
                    return ChatResult(generations=[ChatGeneration(
                        message=AIMessage(content="No session reference was provided."))])
                reply = self._tool("contact_session", {
                    "target": target[1], "request": "Read /FACT.txt and deliver its marker.",
                })
            return ChatResult(generations=[ChatGeneration(message=reply)])

        @staticmethod
        def _tool(name: str, args: dict[str, str]) -> Any:
            return AIMessage(content="", tool_calls=[{
                "id": f"call-{name}", "type": "tool_call", "name": name,
                "args": {"intent": "verify collaboration", **args},
            }])

    return ScriptedModel(responses=[])


async def publish_fixtures(root: Path, target_file: Path) -> dict[str, Any]:
    """Register the fixture projects and create one session in each.

    The console can only offer a session the sidebar has loaded, so both rows
    must exist before the browser run starts.  Everything goes through the real
    wire methods; no database is written by hand.
    """
    from websockets.asyncio.client import connect as ws_connect

    state = root / "daemon"
    metadata = json.loads((state / "daemon.json").read_text(encoding="utf-8"))
    token = (state / "token").read_text(encoding="utf-8").strip()
    marker = (root / "target" / "FACT.txt").read_text(encoding="utf-8").strip()
    async with ws_connect(
        f"ws://127.0.0.1:{metadata['port']}",
        additional_headers={"Authorization": f"Bearer {token}"},
        max_size=8 * 1024 * 1024,
    ) as ws:
        client = Client(ws)
        await client.call("runtime.protocol.negotiate", {"versions": ["1"]})
        sessions: dict[str, Any] = {}
        for name in ("source", "target"):
            project = await client.call("runtime.project.register", {
                "workspace_path": str(root / name),
            })
            created = await client.call("runtime.session.create", {
                "project_id": project["project_id"],
                "title": f"UI {name}",
            })
            sessions[name] = created["session"]
    payload = {"marker": marker, "sessions": sessions}
    target_file.write_text(json.dumps(payload), encoding="utf-8")
    log(f"fixtures published: {target_file}")
    return payload


async def serve(
    root: Path, *, scripted: bool = False, gate: Path | None = None,
    fixtures: Path | None = None,
) -> None:
    """Use the production daemon, overriding only its catalog's storage path."""
    from synapse.runtime.daemon.application import RuntimeDaemon
    from synapse.runtime.daemon.config import DaemonConfig
    from synapse.settings import load_global_settings

    def settings(workspace: Any = root) -> Any:
        """Isolated per-project snapshot for the scripted daemon.

        Explicit rather than layered: the user's own settings and model catalog
        must not leak into a fixture run, and read-only keeps the scripted turn
        from touching any file.
        """
        from synapse.settings import Settings

        workspace = Path(workspace)
        return Settings(
            _env_file=None, workspace=workspace, model="openai:scripted",
            active_model="scripted", models_json=None, models_config_path=None,
            project_catalog_path=root / "catalog.sqlite",
            checkpoint_path=workspace / ".synapse" / "checkpoints.sqlite",
            sessions_path=workspace / ".synapse" / "sessions.sqlite",
            safety_profile="readonly", enable_mcp=False, enable_subagents=False,
            enable_custom_subagents=False, enable_goals=False, enable_memory=False,
            skills_paths=[],
        )

    def agent(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unreachable")

    daemon = RuntimeDaemon(
        DaemonConfig(state_dir=root / "daemon", port=0),
        settings_factory=settings if scripted else (
            lambda: load_global_settings(project_catalog_path=root / "catalog.sqlite")
        ),
    )
    patches: list[Any] = []
    if scripted:
        # The *original* builder is captured before the patch lands: looking the
        # attribute up inside the wrapper would find the wrapper itself.
        from synapse.app.agent import build_coding_agent as build_agent

        patches = [
            patch_target("synapse.runtime.daemon.application.load_project_settings", settings),
            patch_target(
                "synapse.app.agent.build_coding_agent",
                scripted_builder(build_agent, gate),
            ),
        ]
    try:
        await daemon.start()
        if fixtures is not None:
            await publish_fixtures(root, fixtures)
        while not (root / "stop").exists():
            await asyncio.sleep(0.2)
    finally:
        for patch_object in reversed(patches):
            patch_object.stop()
        await daemon.shutdown()


def scripted_builder(build_agent: Any, gate: Path | None) -> Any:
    """Wrap the real agent builder so only its chat model is replaced."""

    def build(*args: Any, **kwargs: Any) -> Any:
        from synapse.models.profile import ModelProfile
        from synapse.models.registry import ModelRegistry

        return build_agent(
            *args, **kwargs, model=scripted_model(gate),
            model_registry=ModelRegistry(default="scripted", profiles={
                "scripted": ModelProfile(name="scripted", model="openai:scripted"),
            }),
            system_prompt="Read-only collaboration acceptance. Use the collaboration tools.",
        )

    return build


def patch_target(target: str, replacement: Any) -> Any:
    """Return a started monkeypatch, without depending on pytest at runtime."""
    from unittest.mock import patch as _patch

    patch_object = _patch(target, replacement)
    patch_object.start()
    return patch_object


class Process:
    def __init__(
        self, root: Path, *, server_entry: Path | None = None, server_args: list[str] | None = None,
    ) -> None:
        self.root = root
        self.server_entry = server_entry or Path(__file__).resolve()
        self.server_args = list(server_args or [])
        self.proc: subprocess.Popen | None = None
        self.output: Any = None

    async def start(self) -> dict[str, Any]:
        (self.root / "stop").unlink(missing_ok=True)
        self.output = (self.root / "daemon.log").open("a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(self.server_entry), *self.server_args, "--serve", str(self.root)],
            cwd=ROOT, stdout=self.output, stderr=subprocess.STDOUT,
        )
        path = self.root / "daemon" / "daemon.json"
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("isolated daemon exited during startup; see its local log")
            if path.is_file():
                metadata = json.loads(path.read_text(encoding="utf-8"))
                log(f"isolated daemon pid={metadata['pid']} port={metadata['port']}")
                return metadata
            await asyncio.sleep(0.2)
        raise TimeoutError("isolated daemon startup timed out")

    async def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        (self.root / "stop").touch()
        try:
            await asyncio.to_thread(proc.wait, timeout=25)
        except subprocess.TimeoutExpired:
            # Windows's venv python.exe is a redirector with a child interpreter.
            # Terminating only that launcher leaves a live daemon behind.
            if os.name == "nt":
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                )
            else:
                proc.kill()
            await asyncio.to_thread(proc.wait, timeout=10)
        finally:
            self.output.close()
            self.proc = None


def connection(root: Path, metadata: dict[str, Any]) -> Any:
    token = (root / "daemon" / "token").read_text(encoding="utf-8").strip()
    return connect(
        f"ws://127.0.0.1:{metadata['port']}",
        additional_headers={"Authorization": f"Bearer {token}"},
        max_size=8 * 1024 * 1024,
    )


def prepare(root: Path) -> str:
    """Persist only generated fixtures and non-secret test settings."""
    marker = "COLLABORATION_OK_" + uuid.uuid4().hex[:12]
    for name in ("source", "target"):
        project = root / name
        config = project / ".synapse"
        config.mkdir(parents=True)
        (config / "settings.json").write_text(json.dumps({
            "checkpoint_backend": "sqlite",
            "checkpoint_path": str(config / "checkpoints.sqlite"),
            "sessions_path": str(config / "sessions.sqlite"),
            "project_catalog_path": str(root / "catalog.sqlite"),
            "safety_profile": "readonly",
            "enable_subagents": False, "enable_custom_subagents": False,
            "enable_mcp": False, "enable_goals": False, "enable_memory": False,
            "skills_paths": [],
        }), encoding="utf-8")
        (project / "AGENTS.md").write_text(
            "Read-only collaboration acceptance workspace. Do not modify files, "
            "run shell commands, or delegate to subagents.\n", encoding="utf-8",
        )
    (root / "target" / "FACT.txt").write_text(marker + "\n", encoding="utf-8")
    return marker


async def history(client: Client, session: dict[str, str]) -> dict[str, Any]:
    return await client.call("runtime.session.history", {"session": session, "limit": 8})


async def exercise(client: Client, root: Path, marker: str, timeout: float) -> tuple:
    refs: dict[str, dict[str, str]] = {}
    for name in ("source", "target"):
        project = await client.call("runtime.project.register", {
            "workspace_path": str(root / name),
        })
        created = await client.call("runtime.session.create", {
            "project_id": project["project_id"], "title": f"collaboration acceptance: {name}",
        })
        refs[name] = created["session"]
        # Open for event subscription without starting a turn.
        await client.call("runtime.session.open", {"session": refs[name]})
        await client.watch(name, refs[name])
        snapshot = await client.call("runtime.session.get", {"session": refs[name]})
        log(f"{name}: default model={snapshot.get('model')}")
    source, target = refs["source"], refs["target"]
    target_id = f"{target['project_id']}:{target['thread_id']}"
    prompt = (
        f"请用 contact_session 联系 @session:{target_id}，请对方读取其工作区 /FACT.txt，"
        "并用 reply_to_collaboration(kind='reply') 原样交付文件中的标记。"
        "你不要自己读取或猜测对方的文件。发出请求后简短说明已发起并结束本轮，不要等待或轮询。"
        "收到协作回复后，在最终答复中原样复述该标记。不要使用子 Agent，不要修改文件。"
    )
    await client.call("runtime.turn.submit", {"session": source, "text": prompt})
    deadline, last = time.monotonic() + timeout, ""
    while time.monotonic() < deadline:
        page = await client.call("runtime.collaboration.list", {"session": source})
        if client.failure:
            raise RuntimeError(client.failure)
        requests = page["requests"]
        entry = requests[0] if requests else None
        status = (
            f"{entry['state']} pending={entry['pending']}" if entry else "waiting for request"
        )
        if status != last:
            log(status)
            last = status
        if entry and entry["state"] in ("failed", "cancelled"):
            raise RuntimeError(f"collaboration ended: {entry['state']}")
        if not entry and client.completed.get("source", 0):
            raise RuntimeError(
                "source completed without a durable request "
                f"(observed tool calls={client.tool_calls.get('source', 0)})"
            )
        if entry and entry["state"] == "replied" and entry["pending"] == 0:
            source_history = await history(client, source)
            target_history = await history(client, target)
            answers = [e for e in source_history["events"] if e["kind"] == "answer"]
            if source_history["total_turns"] >= 2 and answers and marker in answers[-1]["text"]:
                assert target_history["total_turns"] == 1
                assert any(m["kind"] == "reply" and marker in m["body"] for m in entry["messages"])
                log("PASS: target replied and source completed its automatic continuation")
                return refs, entry["request_id"], source_history, target_history
        await asyncio.sleep(0.5)
    raise TimeoutError(f"acceptance timed out: {last}; phases={client.phases}")


async def verify(
    timeout: float, *, server_entry: Path | None = None, root: Path | None = None,
    server_args: list[str] | None = None,
) -> None:
    if root is None:
        root = ROOT / ".tmp" / "collaboration-check" / uuid.uuid4().hex[:12]
    root.mkdir(parents=True, exist_ok=True)
    marker = prepare(root)
    log(f"artifacts: {root.relative_to(ROOT) if root.is_relative_to(ROOT) else root.name}")
    process = Process(root, server_entry=server_entry, server_args=server_args)
    try:
        metadata = await process.start()
        async with connection(root, metadata) as ws:
            client = Client(ws)
            await client.call("runtime.protocol.negotiate", {"versions": ["1"]})
            result = await exercise(client, root, marker, timeout)
        # No open client remains. Restart the same mailbox and fixture projects.
        await process.stop()
        metadata = await process.start()
        refs, request_id, old_source, old_target = result
        async with connection(root, metadata) as ws:
            client = Client(ws)
            page = await client.call("runtime.collaboration.list", {"session": refs["source"]})
            assert len(page["requests"]) == 1
            assert page["requests"][0]["request_id"] == request_id
            assert page["requests"][0]["state"] == "replied"
            assert page["requests"][0]["pending"] == 0
            await asyncio.sleep(1)
            for name, old in (("source", old_source), ("target", old_target)):
                restored = await history(client, refs[name])
                assert restored["total_turns"] == old["total_turns"]
                assert restored["events"] == old["events"]
            log("PASS: daemon restart preserved both transcripts and did not replay the request")
    finally:
        await process.stop()
        log("isolated daemon stopped; existing daemon untouched")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--prepare", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--scripted", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--gate", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fixtures", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--serve", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.prepare is not None:
            args.prepare.mkdir(parents=True, exist_ok=True)
            marker = prepare(args.prepare)
            print(json.dumps({"marker": marker, "root": str(args.prepare)}), flush=True)
        elif args.serve is not None:
            asyncio.run(serve(
                args.serve, scripted=args.scripted, gate=args.gate, fixtures=args.fixtures,
            ))
        else:
            asyncio.run(verify(args.timeout))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # Do not dump arbitrary provider exceptions that may contain credentials.
        if type(exc) in (RuntimeError, TimeoutError, AssertionError):
            log(f"FAIL: {type(exc).__name__}: {str(exc)[:400]}")
        else:
            log(f"FAIL: {type(exc).__name__}; inspect the private local artifacts")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
