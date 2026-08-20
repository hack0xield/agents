"""Settings, all from the environment (spec §47 — no secrets in files)."""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://trading:trading@127.0.0.1:5432/trading_assistant"
)
# A bare postgresql:// URL makes SQLAlchemy reach for psycopg2; we ship
# psycopg 3. Normalise rather than demanding a particular spelling in .env.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8081/mcp")

# §8: Sonnet is the normal brain. Haiku for classification and summarisation
# once those exist; Opus stays the exception.
MODEL = os.environ.get("ORCHESTRATOR_MODEL", "claude-sonnet-5")
MAX_TOKENS = int(os.environ.get("ORCHESTRATOR_MAX_TOKENS", "8000"))

# "anthropic" | "stub". stub answers deterministically without an API key, so
# the adapter, identity, storage and audit paths can be tested end to end with
# no credits and no spend.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")

# §26: how much history goes back to the model before compaction is needed.
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "20"))

WORKSPACE = REPO / "agent-workspace"
PROMPT_VERSION = os.environ.get("PROMPT_VERSION", "soul-2026-08-20")
