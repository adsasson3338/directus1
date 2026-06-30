"""
shared.py — Unified infrastructure for discovery and ingestion pipelines.
One session store, direct Postgres and AI calls, webhook only for file fetch.
"""
import os
import json
import time
import asyncio
import urllib.request
import uuid
from datetime import date, timedelta

import asyncpg
import httpx

from config import (
    DATABASE_URL,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    OPENROUTER_MAX_TOKENS,
    N8N_FETCH_FILE_WEBHOOK,
    JOB_TIMEOUT_SECONDS,
    SESSION_TIMEOUT_SECONDS,
    SESSION_GRACE_SECONDS,
)

# ─────────────────────────────────────────────
# VALIDATION HELPERS (shared by discovery and ingestion)
# ─────────────────────────────────────────────

def _validate_uuid(v: str) -> str:
    """Validate UUID format — raises ValueError if invalid."""
    return str(uuid.UUID(str(v)))


def _validate_date(v: str) -> str:
    """Validate ISO date string — raises ValueError if invalid."""
    date.fromisoformat(str(v))
    return str(v)

# ─────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────
_sessions: dict = {}  # session_id -> session dict
_jobs: dict     = {}  # job_id -> {session_id, stage, ...}  (fetch-file only)

# ─────────────────────────────────────────────
# POSTGRES
# ─────────────────────────────────────────────

async def call_postgres(sql: str) -> list:
    """Execute SQL and return list of row dicts. Raises on error."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(sql)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ─────────────────────────────────────────────
# AI
# ─────────────────────────────────────────────

async def call_ai(prompt: str) -> str:
    """Call OpenRouter and return the response text. Raises on error."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":      OPENROUTER_MODEL,
                "max_tokens": OPENROUTER_MAX_TOKENS,
                "messages":   [{"role": "user", "content": prompt}],
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


# ─────────────────────────────────────────────
# FETCH FILE WEBHOOK (n8n — MinIO only)
# ─────────────────────────────────────────────

async def fire_fetch_file_webhook(job_id: str, file_audit_id: str) -> str | None:
    """Fire the n8n fetch-file webhook. Returns error string or None."""
    body = json.dumps({"job_id": job_id, "file_audit_id": file_audit_id}).encode()
    def _send():
        try:
            req = urllib.request.Request(
                N8N_FETCH_FILE_WEBHOOK, data=body,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=10)
            return None
        except Exception as e:
            return str(e)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _send)


# ─────────────────────────────────────────────
# DATE HELPER
# ─────────────────────────────────────────────

def normalize_to_saturday(d: date) -> date:
    """Return the Saturday of the week containing d (Mon=0, Sat=5)."""
    days_to_saturday = (5 - d.weekday()) % 7
    return d + timedelta(days=days_to_saturday)


# ─────────────────────────────────────────────
# CLEANUP TASK
# ─────────────────────────────────────────────

async def cleanup_stale_jobs():
    """Evict stale fetch-file jobs and expired sessions."""
    while True:
        await asyncio.sleep(60)
        now = time.time()

        # Expire stale fetch-file jobs
        stale_job_ids = [
            jid for jid, j in list(_jobs.items())
            if now - j.get("created_at", now) > JOB_TIMEOUT_SECONDS
        ]
        for jid in stale_job_ids:
            job = _jobs.pop(jid, {})
            sid = job.get("session_id")
            if sid and sid in _sessions:
                session = _sessions[sid]
                session["_pending_jobs"].discard(jid)
                session.setdefault("errors", []).append(
                    f"File fetch job {jid} timed out after {JOB_TIMEOUT_SECONDS}s"
                )

        # Expire stale in-progress sessions
        stale_session_ids = [
            sid for sid, s in list(_sessions.items())
            if now - s.get("created_at", now) > SESSION_TIMEOUT_SECONDS
            and s.get("status") not in ("complete", "failed")
        ]
        for sid in stale_session_ids:
            session = _sessions.get(sid, {})
            session["stage"]  = "failed"
            session["status"] = "failed"
            session["result"] = {"error": "Session timed out", "errors": session.get("errors", [])}

        # Evict completed/failed sessions after grace period
        evict_ids = [
            sid for sid, s in list(_sessions.items())
            if s.get("status") in ("complete", "failed")
            and now - s.get("created_at", now) > SESSION_GRACE_SECONDS
        ]
        for sid in evict_ids:
            _sessions.pop(sid, None)
