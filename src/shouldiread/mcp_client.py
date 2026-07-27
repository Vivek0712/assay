"""Read the scored corpus back out through our own MCP server.

The published leaderboard is built from this rather than from a direct DynamoDB
read, which means the site is a client of the same MCP surface third parties
connect to. That is worth doing for a reason beyond neatness: if the MCP tools
ever drift from what the web surfaces show - a different filter, a lapsed
attribution rule - the publish step breaks loudly instead of the two quietly
disagreeing.

Two transports:

* stdio    - spawns `python -m shouldiread.mcp_server` locally. The default,
             because publishing runs on a laptop or in CI.
* agentcore - SigV4-signed streamable HTTP against the deployed runtime, for
             proving the hosted server serves the same data.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.parse
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_RUNTIME_ARN = os.environ.get(
    "SIR_MCP_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:643603452951:runtime/shouldiread_mcp-4iAgDjBOFc",
)


# --------------------------------------------------------------------------
# stdio
# --------------------------------------------------------------------------
async def _call_stdio(tool: str, arguments: dict[str, Any], env: dict[str, str]) -> Any:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "shouldiread.mcp_server"],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            if not result.content:
                raise RuntimeError(f"{tool} returned no content")
            return json.loads(result.content[0].text)


# --------------------------------------------------------------------------
# AgentCore Runtime
# --------------------------------------------------------------------------
def _call_agentcore(tool: str, arguments: dict[str, Any], *, runtime_arn: str) -> Any:
    """One SigV4-signed MCP round trip against the deployed runtime."""
    import boto3
    import httpx
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    region = runtime_arn.split(":")[3]
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{urllib.parse.quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT"
    )
    session = boto3.Session(
        profile_name=os.environ.get("SIR_AWS_PROFILE") or None, region_name=region
    )
    credentials = session.get_credentials().get_frozen_credentials()

    def rpc(payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload)
        request = AWSRequest(
            method="POST",
            url=url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(request)
        response = httpx.post(url, content=body, headers=dict(request.headers), timeout=180)
        response.raise_for_status()
        text = response.text
        if "data:" in text:  # server-sent-events framing
            text = "\n".join(
                line[5:].strip() for line in text.splitlines() if line.startswith("data:")
            )
        return json.loads(text)

    rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "assay-publish", "version": "1"},
            },
        }
    )
    result = rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    if "error" in result:
        raise RuntimeError(f"{tool} failed: {result['error']}")
    return json.loads(result["result"]["content"][0]["text"])


# --------------------------------------------------------------------------
def load_corpus_via_mcp(
    *,
    transport: str = "stdio",
    run: str = "week",
    scores_table: str | None = None,
    prefs_table: str | None = None,
    runtime_arn: str = DEFAULT_RUNTIME_ARN,
    min_rqs: float = 0.0,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """The scored corpus, fetched through the MCP `export_corpus` tool.

    Records already have the attribution policy applied by the server.
    """
    import asyncio

    arguments = {"min_rqs": min_rqs, "limit": limit}

    if transport == "agentcore":
        payload = _call_agentcore("export_corpus", arguments, runtime_arn=runtime_arn)
    else:
        env = {"SIR_RUN": run}
        # Point the spawned server at DynamoDB when a table is configured, so
        # the publish step reads exactly what the hosted server would serve.
        for key, value in (("SCORES_TABLE", scores_table), ("PREFS_TABLE", prefs_table)):
            if value:
                env[key] = value
        for key in ("AWS_PROFILE", "AWS_REGION", "SIR_AWS_PROFILE", "SIR_AWS_REGION"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        payload = asyncio.run(_call_stdio("export_corpus", arguments, env))

    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(payload["error"])

    scores = payload.get("scores", [])
    log.info(
        "loaded %d of %d scores via MCP (%s)",
        len(scores),
        payload.get("corpus_size", len(scores)),
        transport,
    )
    return scores
