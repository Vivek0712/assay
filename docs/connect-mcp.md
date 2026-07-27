# Connecting the ShouldIRead MCP server

The same server runs two ways from one definition — stdio for local clients,
streamable HTTP for anything that talks to a hosted endpoint.

## Tools

| tool | what it does |
|---|---|
| `get_reading_queue` | ranked articles worth reading, filtered by topic, minimum RQS, author kind, age |
| `score_url` | score any Builder Center article on demand, live |
| `explain_score` | full evidence breakdown, including `base_rqs` vs `author_bonus` |
| `search_scored` | search the scored corpus by title, tag, author or headline |
| `set_preferences` / `get_preferences` | persist topic and threshold preferences |
| `corpus_stats` | aggregate statistics — "how much of Builder Center is worth reading?" |

---

## Local (stdio)

For Claude Desktop, Claude Code, or any client that launches a subprocess.

```json
{
  "mcpServers": {
    "shouldiread": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "shouldiread.mcp_server"],
      "env": { "SIR_RUN": "week" }
    }
  }
}
```

With `SCORES_TABLE` set it reads DynamoDB instead of the local run:

```json
"env": {
  "SCORES_TABLE": "ShouldIRead-ScoresTable6CB35494-RKHVY5HKM4E1",
  "PREFS_TABLE": "ShouldIRead-PreferencesTableC3683986-Y6P649M8Y0L8",
  "AWS_PROFILE": "heisenberg"
}
```

Claude Code, in one line:

```bash
claude mcp add shouldiread -- /path/to/.venv/bin/python -m shouldiread.mcp_server
```

---

## Hosted on Bedrock AgentCore Runtime

Deployed and `READY`:

```
arn:aws:bedrock-agentcore:us-east-1:643603452951:runtime/shouldiread_mcp-4iAgDjBOFc
```

The endpoint is the runtime ARN, URL-encoded, under `/runtimes/.../invocations`:

```
https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/<url-encoded-arn>/invocations?qualifier=DEFAULT
```

Requests are **SigV4-signed** against service name `bedrock-agentcore`. The caller
needs `bedrock-agentcore:InvokeAgentRuntime` on that ARN. There is no API key and
no public access — IAM is the only way in.

### Amazon Quick Suite

Add it as a custom MCP connector:

- **Endpoint** — the invocations URL above
- **Authentication** — AWS SigV4, service `bedrock-agentcore`, region `us-east-1`
- **Role** — grant `bedrock-agentcore:InvokeAgentRuntime` on the runtime ARN

Quick Suite negotiates the MCP handshake itself; the seven tools appear once
connected.

### Any other client

Anything that speaks streamable HTTP MCP works, provided it can SigV4-sign. A
minimal signed call:

```python
import json
import urllib.parse

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = "us-east-1"
ARN = "arn:aws:bedrock-agentcore:us-east-1:643603452951:runtime/shouldiread_mcp-4iAgDjBOFc"
URL = (
    f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
    f"{urllib.parse.quote(ARN, safe='')}/invocations?qualifier=DEFAULT"
)

credentials = boto3.Session(region_name=REGION).get_credentials().get_frozen_credentials()


def call(payload: dict) -> dict:
    body = json.dumps(payload)
    request = AWSRequest(
        method="POST",
        url=URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    SigV4Auth(credentials, "bedrock-agentcore", REGION).add_auth(request)
    response = httpx.post(URL, content=body, headers=dict(request.headers), timeout=120)

    text = response.text
    if "data:" in text:  # server-sent events framing
        text = "\n".join(
            line[5:].strip() for line in text.splitlines() if line.startswith("data:")
        )
    return json.loads(text)


call({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "my-client", "version": "1"},
    },
})

queue = call({
    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
    "params": {
        "name": "get_reading_queue",
        "arguments": {"topics": ["amazon-bedrock-agentcore"], "min_rqs": 70, "limit": 5},
    },
})
print(json.loads(queue["result"]["content"][0]["text"]))
```

### One gotcha worth knowing

The MCP Python SDK enables DNS-rebinding protection by default, which validates
the `Host` header. AgentCore rewrites `Host` when it proxies to the container, so
the server answers **421 Misdirected Request** and every call fails — with no
useful error at the client. That protection defends browser-origin attacks; the
only route into an AgentCore runtime is its SigV4-authenticated endpoint, so it
is the wrong control here:

```python
mcp.settings.stateless_http = True   # required by AgentCore
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)
```

---

## No-MCP alternatives

The same data is on the public HTTP API, no signing required:

```bash
curl 'https://d1m5fcrjmmi9ue.cloudfront.net/api/queue?min_rqs=70&topics=bedrock&limit=10'
curl 'https://d1m5fcrjmmi9ue.cloudfront.net/feed.xml?min_rqs=70&topics=serverless'
```

The Atom feed takes the same filters, so any RSS reader becomes a filtered
Builder Center digest.
