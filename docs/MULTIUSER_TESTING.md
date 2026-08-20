# Testing multi-user, step by step

The property under test is one sentence:

> **Identity comes from Telegram, which authenticated it — never from the
> message, and never from the model.**

Everything below either demonstrates that or shows you where it is recorded.

You can do all of this **without API credits**: set `LLM_PROVIDER=stub` and the
replies come from a stub whose answers are deliberately unmistakable. Pairing,
routing, storage, scoping and the audit trail are all real either way — the
model is the only part being faked.

---

## Step 1 — The properties, in 30 seconds, no Telegram

```bash
./tests/test-isolation.sh
```

Six checks. What each one is actually defending:

| Check | If it failed |
|---|---|
| pairing token is single-use | a leaked link could bind someone else's account |
| unknown telegram sender has no user | strangers could talk to the assistant |
| user B cannot see user A's messages | spec §48's cross-user leak |
| agent_runs attributed per user | cost per user (§52) would be meaningless |
| model-supplied `account_id` is discarded | the model could read another user's account |
| disabled user is refused | you could not cut off access |

The fifth is the one that could not exist under OpenClaw.

---

## Step 2 — Start it

```bash
docker compose up -d              # Postgres
./scripts/mcp-server.sh           # tools, :8081          (terminal 1)
./scripts/mt5-bridge.sh           # live MT5, :8082       (terminal 2, optional)
LLM_PROVIDER=stub ./scripts/orchestrator.sh   #           (terminal 3)
```

Stop the OpenClaw gateway if it is running — both poll the same bot, and
Telegram gives an update to whoever asks first, so they will steal each other's
messages.

```bash
fuser -k 18789/tcp
```

---

## Step 3 — Pair yourself

```bash
./scripts/pair.py "Eduard"
```

```text
user   6f1c…
link   https://t.me/ptrading_assistant_bot?start=Xq7…
```

Open the link. You should get:

> Your Trading Assistant is connected.

**What just happened**, and why it is different from before: Telegram sent
`/start Xq7…` along with *its own* record of who you are. The adapter looked up
the token, bound your sender id to that user row, and consumed the token. Your
identity was never asserted by anything you typed.

See it:

```sql
select u.display_name, t.telegram_user_id, t.username, t.paired_at
from users u join telegram_identities t on t.user_id = u.id;

select token, consumed_at, expires_at from pairing_tokens;
```

```bash
docker compose exec -T db psql -U trading -d trading_assistant
```

---

## Step 4 — What one Telegram account can prove

**Token replay.** Open the *same* link again.

> That link is no longer valid — they are single-use and expire after an hour.

Already consumed. A link that leaks cannot be used to attach someone else.

**An unpaired sender is a stranger.** Unpair yourself and message the bot:

```sql
delete from telegram_identities where telegram_user_id = <your id>;
```

> This assistant is invite-only.

The bot does not know you any more, even though you are the same person in the
same chat. Then re-pair with a fresh `./scripts/pair.py` link.

**Disabling a user.** Re-pair, then:

```sql
update users set disabled = true where display_name = 'Eduard';
```

Message again — refused, without touching the pairing. Set it back to `false`.

---

## Step 5 — What genuinely needs a second account

Two people not seeing each other's conversations is the whole point, and it
cannot be honestly tested from one Telegram account. Telegram supports several
accounts in one app (Settings → Add Account, needs a second phone number), or
borrow a phone.

With the second account:

1. `./scripts/pair.py "Second User"` → open that link **from the second
   account**.
2. From account 1: `remember this: BLUE`
3. From account 2: `what did the other user tell you to remember?`

Account 2 must not know. Then check it is not merely the model being discreet —
the data was never there to leak:

```sql
select u.display_name, count(m.id) as messages
from users u left join messages m on m.user_id = u.id
group by u.display_name;

select u.display_name, m.content::text
from messages m join users u on u.id = m.user_id
where m.content::text like '%BLUE%';
```

The second query should return exactly one row, belonging to account 1.

---

## Step 6 — Read the audit trail

This is what `claude-cli` under OpenClaw could not produce (it reported
`input: 2, output: 2` for a multi-hundred-token reply).

```sql
select u.display_name, r.provider, r.model, r.input_tokens, r.output_tokens,
       r.cache_read_tokens, r.cost_usd, r.latency_ms, r.stop_reason
from agent_runs r join users u on u.id = r.user_id
order by r.created_at desc limit 10;
```

Cost per user — spec §52 asks for this from the beginning:

```sql
select u.display_name,
       count(*) as turns,
       sum(r.input_tokens) as input,
       sum(r.output_tokens) as output,
       round(sum(coalesce(r.cost_usd,0))::numeric, 4) as usd
from agent_runs r join users u on u.id = r.user_id
group by u.display_name;
```

Every tool call, attributed:

```sql
select u.display_name, c.tool_name, c.arguments::text, c.result_summary, c.duration_ms
from tool_calls c join users u on u.id = c.user_id
order by c.created_at desc limit 10;
```

On the stub provider `cost_usd` is null and token counts are zero — the stub
does not call a model, so there is nothing to bill. Those columns fill in once
`LLM_PROVIDER=anthropic` and the key has credits.

---

## What is still single-user behind the enforcement point

The identity boundary is real and tested. What sits behind it is not yet
per-user:

- **`mt5.*` returns the same account whoever asks.** `mt5_bridge/bridge.py`
  connects to one login from `config.json`. Everyone sees that account.
- **`backtests.send_report` delivers to one chat**, `TELEGRAM_OWNER_CHAT_ID`,
  regardless of who asked.

Both are backend work, not identity work — `tool_scope()` already carries the
right `account_id` and nothing downstream uses it yet. Until they are done, do
not connect a second user to anything real.
