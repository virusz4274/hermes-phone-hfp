"""Stdio-to-HTTP MCP proxy for the canonical hfp-mcp daemon.

The proxy deliberately contains no BlueZ imports, so spawning it from a generic
MCP client cannot race the daemon for the HFP profile UUID.
"""

from __future__ import annotations
from importlib.metadata import version

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def run_proxy(url: str, bearer_token: str) -> None:
    if not bearer_token:
        raise RuntimeError("HFP_MCP_BEARER_TOKEN is required by the stdio proxy")
    headers = {"Authorization": f"Bearer {bearer_token}"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=False) as client:
        async with streamable_http_client(url, http_client=client) as (
            upstream_read,
            upstream_write,
            _session_id,
        ):
            async with ClientSession(upstream_read, upstream_write) as upstream:
                await upstream.initialize()
                proxy = Server("hfp-mcp-stdio-proxy", version=version('hfp-mcp'))

                @proxy.list_tools()
                async def _list_tools():
                    return (await upstream.list_tools()).tools

                @proxy.call_tool(validate_input=True)
                async def _call_tool(name: str, arguments: dict):
                    return await upstream.call_tool(name, arguments)

                async with stdio_server() as (client_read, client_write):
                    await proxy.run(
                        client_read,
                        client_write,
                        proxy.create_initialization_options(),
                    )


def main(url: str, bearer_token: str) -> None:
    anyio.run(run_proxy, url, bearer_token)
