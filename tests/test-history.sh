#!/usr/bin/env bash
# Conversation history assembly: the token budget, the tool-payload decay, and
# the boundary rule.
#
# The boundary rule is the one that matters. A window that cuts between an
# assistant tool_use and the tool_result answering it produces a hard 400, and
# it fails intermittently — whether it fires depends on where the cut lands,
# which depends on how many tools the last few turns happened to call.
#
# No model, no API key, no spend. Needs only the database.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
if [ -f .env ]; then set -a; source .env; set +a; fi
export LLM_PROVIDER=stub

exec "$REPO/.venv/bin/python" - <<'PY'
import sys, uuid
sys.path.insert(0, "apps/orchestrator")
import config, db, agent, models

db.create_all()
fails = 0
made: list = []


def check(name, ok, detail=""):
    global fails
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


def conversation(s, turns, payload="x" * 400):
    """A conversation of `turns` question/answer pairs, each using one tool."""
    user = models.User(display_name=f"hist-{uuid.uuid4().hex[:8]}")
    s.add(user); s.flush()
    conv = models.Conversation(user_id=user.id)
    s.add(conv); s.flush()
    made.append((user.id, conv.id))
    for i in range(turns):
        s.add(models.Message(conversation_id=conv.id, user_id=user.id, role="user",
                             content=[{"type": "text", "text": f"question {i}"}]))
        s.flush()
        s.add(models.Message(conversation_id=conv.id, user_id=user.id, role="assistant",
                             content=[{"type": "tool_use", "id": f"t{i}",
                                       "name": "mt5.get_account", "input": {}}]))
        s.flush()
        s.add(models.Message(conversation_id=conv.id, user_id=user.id, role="user",
                             content=[{"type": "tool_result", "tool_use_id": f"t{i}",
                                       "content": payload}]))
        s.flush()
        s.add(models.Message(conversation_id=conv.id, user_id=user.id, role="assistant",
                             content=[{"type": "text", "text": f"answer {i}"}]))
        s.flush()
    return conv


def valid_sequence(msgs):
    """Every tool_result must be answered by a tool_use that precedes it."""
    if not msgs:
        return True, ""
    if msgs[0]["role"] != "user":
        return False, f"starts with {msgs[0]['role']}"
    open_ids = set()
    for m in msgs:
        for b in m["content"]:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                open_ids.add(b["id"])
            if b.get("type") == "tool_result":
                if b["tool_use_id"] not in open_ids:
                    return False, f"orphaned tool_result {b['tool_use_id']}"
    return True, ""


with db.session_scope() as s:
    # 1 — empty conversation
    conv = conversation(s, 0)
    check("empty conversation returns nothing", agent._history(s, conv) == [])

    # 2 — short conversation survives whole
    conv = conversation(s, 3)
    msgs = agent._history(s, conv)
    texts = [b.get("text") for m in msgs for b in m["content"] if isinstance(b, dict)]
    check("short conversation keeps its first question", "question 0" in texts,
          f"got {texts}")

    # 3 — the old 20-row window would have dropped this; the budget does not
    conv = conversation(s, 12)          # 48 rows
    msgs = agent._history(s, conv)
    texts = [b.get("text") for m in msgs for b in m["content"] if isinstance(b, dict)]
    check("48 rows: earliest question still present", "question 0" in texts,
          f"{len(msgs)} messages kept")

    # 4 — boundary integrity at every budget, including ones that cut mid-turn
    conv = conversation(s, 25)
    real = config.HISTORY_TOKEN_BUDGET
    bad = []
    for budget in range(60, 4000, 37):
        config.HISTORY_TOKEN_BUDGET = budget
        ok, why = valid_sequence(agent._history(s, conv))
        if not ok:
            bad.append((budget, why))
    config.HISTORY_TOKEN_BUDGET = real
    check("no orphaned tool_result at any budget", not bad,
          f"{len(bad)} bad, first {bad[:1]}")

    # 5 — tool payloads decay, conversation text does not
    conv = conversation(s, 10)
    msgs = agent._history(s, conv)
    full = sum(1 for m in msgs for b in m["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"
               and "superseded" not in str(b.get("content")))
    stub = sum(1 for m in msgs for b in m["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"
               and "superseded" in str(b.get("content")))
    check("only the newest tool results keep their payload",
          full == config.TOOL_RESULTS_KEPT_FULL and stub > 0, f"{full} full, {stub} stubbed")

    # 6 — stubs keep the id, so nothing is orphaned by the stubbing itself
    ok, why = valid_sequence(msgs)
    check("stubbed results still answer their tool_use", ok, why)

    # 7 — the budget is actually enforced
    config.HISTORY_TOKEN_BUDGET = 500
    msgs = agent._history(s, conv)
    est = sum(agent._est_tokens(m["content"]) for m in msgs)
    config.HISTORY_TOKEN_BUDGET = real
    check("history stays inside its budget", est <= 500 + 200, f"~{est} tokens")

    # 8 — cache breakpoint lands on the last block of the prefix, once
    conv = conversation(s, 4)
    msgs = agent._history(s, conv)
    agent._mark_cache_breakpoint(msgs)
    marks = [b for m in msgs for b in m["content"]
             if isinstance(b, dict) and "cache_control" in b]
    check("exactly one cache breakpoint, at the end", len(marks) == 1
          and msgs[-1]["content"][-1] is marks[0], f"{len(marks)} marks")

    # 9 — marking must not dirty the stored rows
    check("breakpoint did not write back to the database",
          not any(isinstance(o, models.Message) for o in s.dirty))

    # 10 — a chatty conversation must not be cut short by the chunk size.
    # This is what a row cap gets wrong: 300 small messages are nowhere near
    # the token budget, so all of them should survive.
    conv = conversation(s, 75, payload="small")     # 300 rows
    msgs = agent._history(s, conv)
    texts = [b.get("text") for m in msgs for b in m["content"] if isinstance(b, dict)]
    check("300 small rows: paging continues past one chunk",
          len(msgs) > config.HISTORY_CHUNK_ROWS and "question 0" in texts,
          f"{len(msgs)} kept, chunk is {config.HISTORY_CHUNK_ROWS}")

    for uid, cid in made:
        s.query(models.Message).filter(models.Message.conversation_id == cid).delete()
        s.query(models.Conversation).filter(models.Conversation.id == cid).delete()
        s.query(models.User).filter(models.User.id == uid).delete()

print()
print("FAILED" if fails else "all history checks passed")
sys.exit(1 if fails else 0)
PY
