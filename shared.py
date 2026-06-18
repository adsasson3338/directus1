"""
shared.py — Unified infrastructure for discovery and ingestion pipelines.
One session store, one job store, one webhook helper.
"""
import os
import json
import uuid
import time
import asyncio
import urllib.request
from datetime import date, timedelta

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
N8N_AI_WEBHOOK           = os.environ.get("N8N_AI_WEBHOOK",           "http://n8n:5678/webhook/ai")
N8N_POSTGRES_WEBHOOK     = os.environ.get("N8N_POSTGRES_WEBHOOK",     "http://n8n:5678/webhook/postgres")
N8N_FETCH_FILE_WEBHOOK   = os.environ.get("N8N_FETCH_FILE_WEBHOOK",   "http://n8n:5678/webhook/fetch-file")

JOB_TIMEOUT_SECONDS     = 300  # 5 minutes per job
SESSION_TIMEOUT_SECONDS = 600

# ─────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────
_sessions: dict = {}  # session_id -> session dict
_jobs: dict     = {}  # job_id -> {session_id, stage, pipeline, ...}

# ─────────────────────────────────────────────
# WEBHOOK HELPER
# ─────────────────────────────────────────────

def fire_webhook(url: str, job_id: str, payload: dict) -> str | None:
    body = json.dumps({"job_id": job_id, **payload}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        return None
    except Exception as e:
        return str(e)

# ─────────────────────────────────────────────
# DATE HELPER
# ─────────────────────────────────────────────

def normalize_to_saturday(d: date) -> date:
    """Return the Saturday of the week containing d (Mon=0, Sat=5)."""
    days_to_saturday = (5 - d.weekday()) % 7
    return d + timedelta(days=days_to_saturday)

# ─────────────────────────────────────────────
# CLEANUP TASK — registered by main.py
# ─────────────────────────────────────────────

# These are set by discovery.py and ingestion.py at import time
_discovery_timeout_handler = None
_ingestion_timeout_handler = None


def register_discovery_timeout_handler(handler):
    global _discovery_timeout_handler
    _discovery_timeout_handler = handler


def register_ingestion_timeout_handler(handler):
    global _ingestion_timeout_handler
    _ingestion_timeout_handler = handler


async def cleanup_stale_jobs():
    while True:
        await asyncio.sleep(60)
        now = time.time()

        stale_job_ids = [
            jid for jid, j in list(_jobs.items())
            if now - j.get("created_at", now) > JOB_TIMEOUT_SECONDS
        ]

        sessions_to_advance = set()
        for jid in stale_job_ids:
            job = _jobs.pop(jid, {})
            sid = job.get("session_id")
            if sid and sid in _sessions:
                session = _sessions[sid]
                session["_pending_jobs"].discard(jid)
                session.setdefault("errors", []).append(
                    f"Job {jid} (stage: {job.get('stage')}) timed out after {JOB_TIMEOUT_SECONDS}s"
                )
                if not session["_pending_jobs"]:
                    sessions_to_advance.add((sid, job.get("stage"), job.get("pipeline", "discovery")))

        for sid, stage, pipeline in sessions_to_advance:
            if sid not in _sessions:
                continue
            if pipeline == "ingestion" and _ingestion_timeout_handler:
                await _ingestion_timeout_handler(sid, stage)
            elif _discovery_timeout_handler:
                await _discovery_timeout_handler(sid, stage)

        # Expire stale sessions
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
