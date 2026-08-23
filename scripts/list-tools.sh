#!/usr/bin/env bash
# Print the tools the agent has, from the source of truth: the MCP server.
#
# Asking the agent itself does not work — SOUL.md forbids it from exposing
# internal names to a user, and it cannot tell the developer apart from a
# customer. That is correct behaviour, so this reads the server instead.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$REPO/.venv/bin/python" - <<'PY'
import asyncio, sys, textwrap
sys.path.insert(0, "mcp_server")
import server

tools = asyncio.run(server.mcp.list_tools())


def writes(t) -> bool:
    a = getattr(t, "annotations", None)
    return getattr(a, "read_only_hint", None) is not True


# Counted from the annotations rather than asserted. This line used to read
# "all read-only", which stopped being true the moment the run and send tools
# were added — and a claim about what the agent can do is exactly the thing
# that must not drift.
rw = [t for t in tools if writes(t)]
print(f"{len(tools)} tools, {len(tools) - len(rw)} read-only, "
      f"{len(rw)} that write\n")
for t in sorted(tools, key=lambda x: x.name):
    exposed = "trading__" + t.name.replace(".", "-")
    print(f"  {t.name}" + ("   [WRITES]" if writes(t) else ""))
    print(f"    model sees: {exposed}")
    first = (t.description or "").strip().split("\n\n")[0].replace("\n", " ")
    print(textwrap.fill(first, 76, initial_indent="    ", subsequent_indent="    "))
    props = (t.input_schema or {}).get("properties", {})
    if props:
        req = set((t.input_schema or {}).get("required", []))
        args = ", ".join(f"{k}{'' if k in req else '?'}" for k in props)
        print(f"    args: {args}")
    print()
PY
