# The MCP server has no authorization

**Status:** known gap, deliberately deferred for the POC.
**Recorded:** 2026-08-20. **Blocks:** any deployment where the process is not
alone on the host.

---

## What is missing

`mcp_server/server.py` authenticates nobody and authorizes nothing. Three
surfaces, all open to whoever can open a socket:

| Surface | What it gives away |
|---|---|
| `POST /mcp` | every tool, including `mt5.*` against any `credential_ref` |
| `GET /artifacts/{run_id}/{path}` | any file in any run directory |
| `GET /bundle/{run_id}.zip` | any run, zipped |

There is no token, no session, no notion of a caller. `HOST = "127.0.0.1"` is
the entire security boundary.

## Why that is currently fine

Two properties hold today, and both are load-bearing:

- **The orchestrator is the only client.** Identity is enforced one layer up,
  in `apps/orchestrator/tools.py`, which overwrites `account_id`,
  `credential_ref` and `chat_id` from the caller's own database rows *after*
  the model has spoken. The MCP server is handed a scope it never has to
  verify because it cannot be reached by anyone who could forge one.
- **Loopback on a single-tenant host.** One developer machine, one user
  account, nothing else listening.

Neither property survives the first real deployment.

## Exactly when this becomes a vulnerability

Any one of these is sufficient. None is exotic:

- The process moves to a shared or multi-tenant host — any local process, any
  other container in the same network namespace, gets full MT5 read access to
  every connected trader's account.
- The port is exposed for debugging, or a reverse proxy is put in front of it.
- A second client is added — a web dashboard, a mobile app, a cron job — and
  the "only the orchestrator can call this" assumption stops being true
  without anyone deciding to change it.
- An SSRF anywhere else on the host turns into full account disclosure,
  because `127.0.0.1:8081` is exactly what an SSRF can reach.

The artifact routes are the softest of the three: `run_id` is guessable from
any report the assistant has ever sent, and there is no ownership check
whatsoever. Spec §48 wants tenant isolation; these routes do not know tenants
exist.

## What to build

In rough order of value per hour:

**1. A shared secret on `/mcp`.** The orchestrator holds it, the server
requires it. One env var, one header check, maybe twenty lines. This does not
give per-user authorization but it removes "any local process" from the threat
model, which is the biggest single reduction available.

**2. Signed, expiring artifact URLs.** `run_id` alone must stop being
sufficient. An HMAC over `(run_id, path, expiry)` issued by the orchestrator,
verified by the server, keeps the URL space stable while making it
unguessable. Delivery currently pushes files into the chat rather than sharing
links, so this is cheap to introduce before any link is public.

**3. Caller identity on every tool call.** The real fix, and the one that
matches §48. The MCP server should receive the calling user's id, verify the
`credential_ref` belongs to that user against the database, and refuse
otherwise. That makes the tool layer defend itself rather than trusting its
only current client — the same reason `tools.py` overwrites scope rather than
asking the model to behave.

**4. Ownership on artifacts.** Once (3) exists, runs can be checked against the
requesting user rather than served to anyone who names them.

## What not to do

Do not move enforcement *out* of `apps/orchestrator/tools.py` when adding it
here. That function is the reason a model cannot ask for someone else's
account, it is covered by `tests/test-isolation.sh`, and defence in depth is
the point: the tool layer verifying identity does not make the orchestrator's
overwrite redundant, it makes a bug in either one survivable.

## Related

- `tests/test-isolation.sh` — nine checks, all at the orchestrator layer
- `docs/MT5.md` §3b — the bridge's own single-account limitation
- Spec §47 (credentials outside Postgres), §48 (tenant isolation)
