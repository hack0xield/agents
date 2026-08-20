"""Assemble the system prompt.

This is where the 29k of OpenClaw base prompt disappears: we send the product's
own behaviour and the trader's own profile, and nothing else. Measured on
OpenClaw, our files were 4.5k of a 33.9k prompt — the rest was runtime we did
not choose and could not remove.
"""

from __future__ import annotations

import config

# Order matters for prompt caching: stable content first, so the cached prefix
# survives (spec §43). Per-user context is appended after this block.
_FILES = ["SOUL.md", "IDENTITY.md", "AGENTS.md", "TOOLS.md"]


def base_prompt() -> str:
    parts = []
    for name in _FILES:
        f = config.WORKSPACE / name
        if f.is_file():
            parts.append(f"# {name}\n\n{f.read_text().strip()}")
    return "\n\n---\n\n".join(parts)


def user_context(display_name: str | None, profile_md: str | None,
                 accounts: list[dict]) -> str:
    """Per-user block. Kept separate from base_prompt so the stable prefix can
    be cached once and reused across every user (spec §42, §43)."""
    lines = ["# This trader"]
    if display_name:
        lines.append(f"\nYou are talking to {display_name}.")
    if profile_md:
        lines.append("\n" + profile_md.strip())
    if accounts:
        lines.append("\n## Connected accounts\n")
        for a in accounts:
            lines.append(
                f"- {a['nickname']} — login {a['login']} on {a['server']}, "
                f"access {a['access']}, {a['connection_state']}"
            )
    else:
        lines.append("\nNo trading account is connected yet.")
    return "\n".join(lines)
