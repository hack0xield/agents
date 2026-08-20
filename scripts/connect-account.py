#!/usr/bin/env python3
"""Connect an MT5 account to a user — the POC stand-in for spec §3.4.

    ./scripts/connect-account.py --user <uuid> --login 10534821 \
        --server FTMO-Demo2 --nickname "FTMO 100K" --password-stdin

Writes both halves atomically-ish, because half a connection is worse than
none: the credential into mt5_bridge/accounts.json, and the trading_accounts
row that maps a user to it. If the database write fails, the credential is
removed again rather than left orphaned on disk.

What this is NOT: §3.4 wants the trader to type their password into an
authenticated HTTPS page, never into a chat and never into someone else's
terminal. This is an operator tool for founder-alpha, not onboarding.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "apps" / "orchestrator"))

STORE = REPO / "mt5_bridge" / "accounts.json"
WINE_MT5 = r"C:\Program Files\MetaTrader 5\terminal64.exe"


def load_store() -> dict:
    return json.loads(STORE.read_text()) if STORE.is_file() else {}


def save_store(data: dict) -> None:
    STORE.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(STORE, 0o600)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="user id (uuid)")
    ap.add_argument("--login", required=True, type=int)
    ap.add_argument("--server", required=True)
    ap.add_argument("--nickname", required=True)
    ap.add_argument("--broker")
    ap.add_argument("--ref", help="credential ref (default: mt5-<login>)")
    ap.add_argument("--mt5-path", default=WINE_MT5)
    ap.add_argument("--allow-master", action="store_true",
                    help="accept a trading-capable password (spec §3.4 says do not)")
    args = ap.parse_args()

    import db, models  # noqa: E402

    other_users: list = []

    ref = args.ref or f"mt5-{args.login}"
    store = load_store()
    reused = ref in store

    if reused:
        # Attaching a credential that already exists, rather than adding one.
        # Intended for founder-alpha, where a single demo account is shared
        # while the second one does not exist yet.
        entry = store[ref]
        if int(entry.get("login", 0)) != args.login:
            print(f"error: ref {ref!r} is login {entry.get('login')}, not {args.login}",
                  file=sys.stderr)
            return 1
        print(f"reusing existing credential {ref!r} (no password needed)")
    else:
        password = getpass.getpass("MT5 investor password: ")
        if not password:
            print("error: empty password", file=sys.stderr)
            return 1
        store[ref] = {
            "login": args.login,
            "password": password,
            "server": args.server,
            "mt5_path": args.mt5_path,
            **({"allow_master": True} if args.allow_master else {}),
        }
        save_store(store)

    try:
        db.create_all()
        with db.session_scope() as s:
            user = s.get(models.User, uuid.UUID(args.user))
            if user is None:
                raise SystemExit(f"no such user: {args.user}")
            existing = [a for a in user.trading_accounts]
            other_users[:] = s.query(models.TradingAccount).filter(
                models.TradingAccount.credential_ref == ref,
                models.TradingAccount.user_id != user.id).all()
            s.add(models.TradingAccount(
                user_id=user.id, nickname=args.nickname, login=args.login,
                server=args.server, broker=args.broker, credential_ref=ref,
                access="master_TRADING_ENABLED" if args.allow_master else "investor_read_only",
                is_default=not existing,
            ))
    except BaseException:
        if not reused:
            # Roll back only what this run wrote. An orphan secret with no row
            # pointing at it is a liability nobody remembers to clean up — but
            # a pre-existing credential is not ours to delete.
            store.pop(ref, None)
            save_store(store)
        raise

    if reused and other_users:
        print(f"note: credential {ref!r} is now attached to {len(other_users) + 1} users. "
              f"They share one MT5 account — fine for testing, wrong for real traders.")
    print(f"connected {args.nickname} (login {args.login}) to user {args.user}")
    print(f"  credential_ref: {ref}")
    print("  the bridge picks this up without a restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
