"""Run Synapse as an ACP Agent over stdio."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from acp.agent.connection import AgentSideConnection
from acp.agent.router import AGENT_METHODS, normalize_result
from acp.schema import DeleteSessionRequest

from synapse.acp.agent import SynapseACPAgent


def build_agent_connection(
    agent: SynapseACPAgent,
    input_stream: object,
    output_stream: object | None = None,
    *,
    use_unstable_protocol: bool = False,
    **connection_kwargs: object,
) -> AgentSideConnection:
    """Build the SDK connection and patch the 0.12.0 delete-route omission.

    ``agent-client-protocol==0.12.0`` exposes ``session/delete`` in
    ``AGENT_METHODS`` but does not register it in ``build_agent_router``. Keep
    this compatibility fix at the transport boundary so the domain Agent does
    not depend on a forked SDK.
    """
    connection = AgentSideConnection(
        agent,
        input_stream,
        output_stream,
        use_unstable_protocol=use_unstable_protocol,
        listening=bool(connection_kwargs.pop("listening", False)),
        **connection_kwargs,
    )
    router = getattr(getattr(connection, "_conn", None), "_handler", None)
    routes = getattr(router, "_requests", {})
    method = AGENT_METHODS["session_delete"]
    if method not in routes:
        router.route_request(
            method,
            DeleteSessionRequest,
            agent,
            "session_delete",
            adapt_result=normalize_result,
            unstable=True,
        )
    return connection


def _configure_logging() -> None:
    """Keep diagnostics off stdout, which is reserved for ACP JSON-RPC."""
    logging.basicConfig(
        level=logging.INFO if os.environ.get("SYNAPSE_ACP_LOG") else logging.WARNING,
        stream=sys.stderr,
        format="[synapse-acp] %(levelname)s %(message)s",
    )


async def run_server(agent: SynapseACPAgent | None = None) -> None:
    """Run one ACP stdio server until the client closes the connection."""
    _configure_logging()
    instance = agent or SynapseACPAgent()
    from acp.stdio import stdio_streams

    # ``stdio_streams`` returns (reader, writer), while AgentSideConnection
    # accepts the writer as its input stream and reader as its output stream.
    reader, writer = await stdio_streams(
        limit=50 * 1024 * 1024
    )
    connection = build_agent_connection(
        instance,
        writer,
        reader,
        use_unstable_protocol=True,
    )
    try:
        await connection.listen()
    finally:
        await asyncio.shield(connection.close())
        await instance.shutdown()


def main() -> None:
    """Console-script entry point."""
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
