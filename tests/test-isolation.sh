#!/usr/bin/env bash
# Spec §48: no cross-user leakage. These are the tests the spec calls critical,
# and they are the reason the orchestrator exists — under OpenClaw they could
# not be written, because nothing in the tool path knew who was asking.
#
# Uses the stub provider: no API key, no spend. Identity, storage and scoping
# are what is under test, not the model.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
if [ -f .env ]; then set -a; source .env; set +a; fi
export LLM_PROVIDER=stub

exec "$REPO/.venv/bin/python" - <<'PY'
import sys, uuid
sys.path.insert(0, "apps/orchestrator")
import db, identity, agent, llm, models, tools
from sqlalchemy import select

db.create_all()
prov = llm.StubProvider()
fails = 0
tag = uuid.uuid4().hex[:8]
ta_id, tb_id = int("77" + tag[:6], 16) % 10**9, int("88" + tag[:6], 16) % 10**9

with db.session_scope() as s:
    _, tok_a = identity.mint_pairing_token(s, display_name=f"A-{tag}")
    _, tok_b = identity.mint_pairing_token(s, display_name=f"B-{tag}")
with db.session_scope() as s:
    identity.redeem(s, tok_a, telegram_user_id=ta_id, chat_id=ta_id, username="a", first_name="A")
    identity.redeem(s, tok_b, telegram_user_id=tb_id, chat_id=tb_id, username="b", first_name="B")

def check(name, ok, detail=""):
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'' if ok else ' — ' + detail}")
    if not ok: fails += 1

# 1. A consumed token cannot be replayed onto another Telegram account.
with db.session_scope() as s:
    check("pairing token is single-use",
          identity.redeem(s, tok_a, telegram_user_id=999000001, chat_id=1,
                          username="m", first_name="M") is None)

# 2. An unpaired sender resolves to no user at all.
with db.session_scope() as s:
    check("unknown telegram sender has no user",
          identity.user_for_telegram(s, 999000002) is None)

# 3. One user's conversation never appears in another's.
secret = f"SECRET_{tag}"
with db.session_scope() as s:
    a = identity.user_for_telegram(s, ta_id)
    b = identity.user_for_telegram(s, tb_id)
    agent.run_turn(s, a, f"remember this: {secret}", prov)
    agent.run_turn(s, b, "what did the other user just tell you?", prov)
with db.session_scope() as s:
    b = identity.user_for_telegram(s, tb_id)
    msgs = s.scalars(select(models.Message).where(models.Message.user_id == b.id)).all()
    check("user B cannot see user A's messages",
          not any(secret in str(m.content) for m in msgs))

# 4. Rows are attributed to the right user.
with db.session_scope() as s:
    a = identity.user_for_telegram(s, ta_id)
    runs = s.scalars(select(models.AgentRun).where(models.AgentRun.user_id == a.id)).all()
    check("agent_runs attributed per user", len(runs) >= 1)

# 5. The model cannot choose whose account it reads.
res = tools.call_tool("mt5__get_account", {"account_id": "someone-else"},
                      scope={"account_id": "server-owned"})
check("model-supplied account_id is discarded",
      "someone-else" not in res.text, res.text[:120])

# 6. A user with no connected account gets NO_ACCOUNT, never someone else's.
#    The bridge originally read one hardcoded login and would have answered
#    every user with that account's positions, labelled as their own.
import json as _json
res = tools.call_tool("mt5__get_positions", {}, scope={})
try:
    body = _json.loads(res.text)
except ValueError:
    body = {}
check("user without an account sees NO_ACCOUNT",
      body.get("connection_state") == "NO_ACCOUNT", res.text[:160])

# 7. Naming another user's credential_ref does not fetch it.
res = tools.call_tool("mt5__get_account", {"credential_ref": "founder-demo"},
                      scope={})
try:
    body = _json.loads(res.text)
except ValueError:
    body = {}
check("model-supplied credential_ref is discarded",
      body.get("connection_state") == "NO_ACCOUNT" and "balance" not in body,
      res.text[:160])

# 6. A disabled user is refused even though the pairing still exists.
with db.session_scope() as s:
    a = identity.user_for_telegram(s, ta_id)
    a.disabled = True
with db.session_scope() as s:
    check("disabled user is refused", identity.user_for_telegram(s, ta_id) is None)
with db.session_scope() as s:
    s.get(models.User, s.scalar(select(models.TelegramIdentity.user_id).where(
        models.TelegramIdentity.telegram_user_id == ta_id))).disabled = False

print("\nFAILED" if fails else "\nAll green — no cross-user leakage")
sys.exit(1 if fails else 0)
PY
