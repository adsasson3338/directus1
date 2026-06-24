"""
config.py — External connection config and secrets.
All values read from environment variables.
Add this file to .gitignore — never commit credentials.
"""
import os

# ─────────────────────────────────────────────
# POSTGRES
# ─────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ─────────────────────────────────────────────
# OPENROUTER / AI
# ─────────────────────────────────────────────
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL",   "qwen/qwen3-30b-a3b-thinking-2507")
OPENROUTER_MAX_TOKENS = int(os.environ.get("OPENROUTER_MAX_TOKENS", "4000"))

# ─────────────────────────────────────────────
# N8N — only fetch-file webhook remains
# ─────────────────────────────────────────────
N8N_FETCH_FILE_WEBHOOK = os.environ.get("N8N_FETCH_FILE_WEBHOOK", "http://n8n:5678/webhook/fetch-file")

# ─────────────────────────────────────────────
# PIPELINE TUNING
# ─────────────────────────────────────────────
JOB_TIMEOUT_SECONDS     = int(os.environ.get("JOB_TIMEOUT_SECONDS",     "300"))
SESSION_TIMEOUT_SECONDS = int(os.environ.get("SESSION_TIMEOUT_SECONDS", "600"))
SESSION_GRACE_SECONDS   = int(os.environ.get("SESSION_GRACE_SECONDS",   "900"))
MAX_UPLOAD_BYTES        = int(os.environ.get("MAX_UPLOAD_BYTES",        str(50 * 1024 * 1024)))
