#!/usr/bin/env python3
"""Mint a pairing link (spec §3.3).

    ./scripts/pair.py "Eduard"

Prints a t.me deep link. Single use, expires in an hour.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "orchestrator"))

import config      # noqa: E402
import db          # noqa: E402
import identity    # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else None
db.create_all()
with db.session_scope() as s:
    user, token = identity.mint_pairing_token(s, display_name=name)
    uid = user.id
bot = config.TELEGRAM_BOT_USERNAME
if not bot:
    # Better to fail than to print a link with a placeholder in it, which
    # looks valid and leads nowhere.
    print("error: TELEGRAM_BOT_USERNAME is not set (check .env)", file=sys.stderr)
    print(f"user   {uid}")
    print(f"token  {token}")
    raise SystemExit(1)
print(f"user   {uid}")
print(f"link   https://t.me/{bot}?start={token}")
