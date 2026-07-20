"""
shared.py — Unified infrastructure for discovery and ingestion pipelines.
One session store, direct Postgres and AI calls, webhook only for file fetch.
"""
import os
import re
import json
import time
import base64
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
    AI_LOG_VERBOSE,
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


def _sql_escape(v) -> str:
    """Escape a value for safe inline SQL string embedding."""
    return str(v).replace("'", "''")


def build_fetch_audit_row_sql(file_audit_id: str) -> str:
    """Fetch a single file_audit row by id — used by both discovery and ingestion."""
    safe_id = _validate_uuid(file_audit_id)
    return f"""
SELECT id, filename, file_hash, minio_path, retailer, status, discovery_result
FROM file_audit
WHERE id = '{safe_id}'
LIMIT 1
""".strip()


# ─────────────────────────────────────────────
# DATE FORMAT PATTERN LIBRARY (Postgres-backed, shared by discovery + ingestion)
# ─────────────────────────────────────────────
# Lives here, not in discovery.py, because BOTH pipelines need to answer the
# same question - "does this header match a known date/period format, and if
# so what date does it represent" - discovery when deciding which columns are
# sales columns, ingestion when actually computing week_ending for real rows.
# One cache, one set of matching/computation functions, used by both; a
# pattern newly discovered by discovery.py's AI escalation is immediately
# usable (once approved) by ingestion.py without any duplicate logic.

_KNOWN_DATE_PATTERNS: list = []  # cached rows: {retailer, pattern_regex, resolution_rule, format_description}


def build_fetch_date_patterns_sql() -> str:
    """All active date/period header patterns, any retailer - evidence that
    a header shape is a real, previously-approved date axis."""
    return "SELECT retailer, pattern_regex, resolution_rule, format_description FROM date_format_patterns WHERE status = 'active'"


def build_insert_date_pattern_sql(retailer: str | None, pattern_regex: str, format_description: str,
                                   resolution_rule: dict, example_header: str, source_file: str) -> str:
    """
    Insert a newly AI-discovered date format pattern as pending_review.
    Scoped to the retailer if known at write time, else NULL - a human can
    assign it to a retailer during review, or leave it universal if the
    format isn't retailer-specific. Stays untrusted (not usable by either
    pipeline) until a human flips status to 'active'.
    """
    retailer_val = f"'{_sql_escape(retailer)}'" if retailer else "NULL"
    rule_b64 = base64.b64encode(json.dumps(resolution_rule, ensure_ascii=False).encode()).decode()
    return f"""
INSERT INTO date_format_patterns
    (retailer, pattern_regex, format_description, resolution_rule, discovered_via, example_header, first_seen_file, status)
VALUES
    ({retailer_val}, '{_sql_escape(pattern_regex)}', '{_sql_escape(format_description)}',
     convert_from(decode('{rule_b64}', 'base64'), 'UTF8')::jsonb,
     'ai', '{_sql_escape(example_header)}', '{_sql_escape(source_file)}', 'pending_review')
""".strip()


async def load_date_patterns(force: bool = False):
    """
    Load/refresh the shared pattern cache from Postgres. Cheap - safe to
    call at the start of any discovery or ingestion run that needs pattern
    matching. Fails soft: if Postgres is briefly unreachable, keeps
    whatever was cached before rather than crashing.
    """
    global _KNOWN_DATE_PATTERNS
    if _KNOWN_DATE_PATTERNS and not force:
        return
    try:
        rows = await call_postgres(build_fetch_date_patterns_sql())
        if rows:
            _KNOWN_DATE_PATTERNS = rows
    except Exception:
        pass


def match_known_patterns(header: str) -> dict | None:
    """Try a header string against the loaded pattern library. Returns the
    matching pattern row (with pattern_regex/resolution_rule/format_description),
    or None if nothing matches - a malformed stored pattern is skipped rather
    than allowed to crash matching for every file."""
    if not isinstance(header, str):
        return None
    s = header.strip()
    for p in _KNOWN_DATE_PATTERNS:
        try:
            if re.match(p["pattern_regex"], s, re.IGNORECASE):
                return p
        except (re.error, TypeError, KeyError):
            continue
    return None


def normalize_header_shape(header: str) -> str:
    """Collapse digits to '#' so headers built the same repeating way compare
    equal regardless of which specific digits they contain - '202601 Units'
    and '202602 Units' both become '###### Units'. Used to group unresolved
    headers by distinct FORMAT before asking AI anything, so AI only needs
    to see and generalize one representative example per shape, not every
    individual column that shares it."""
    return re.sub(r"\d", "#", header.strip())


def compute_date_from_match(match: "re.Match", resolution_rule: dict) -> str | None:
    """
    Computes an actual date from a regex match using an AI-supplied,
    Python-executed method. AI supplies WHICH capture group is the
    year/week (a narrow, one-time judgment call, made once per distinct
    format during discovery's escalation); this function does the actual
    date arithmetic, every time, for whichever column's own header it's
    given - never a value borrowed from a different column's example.

    Only implements methods Python actually knows how to compute. An
    unrecognized method returns None deliberately - the caller (discovery,
    for column enumeration; ingestion, for date resolution) can still use
    the pattern MATCH itself for its own purposes even when no computed
    date is available yet - that's an honest, bounded gap rather than a
    wrong silent answer, and is closed by adding a new branch here, once,
    for both pipelines simultaneously.
    """
    method = resolution_rule.get("method")
    groups = resolution_rule.get("capture_groups", {})
    try:
        if method == "iso_year_week":
            year = int(match.group(groups["year"]))
            week = int(match.group(groups["week"]))
            d = date.fromisocalendar(year, max(1, min(week, 53)), 6)  # Saturday, matching normalize_to_saturday
            return d.isoformat()
    except (KeyError, ValueError, IndexError, Exception):
        return None
    return None

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

async def call_ai(prompt: str, label: str = "") -> str:
    """Call OpenRouter and return the response text. Raises on error."""
    tag = f"[AI:{label}]" if label else "[AI]"

    if AI_LOG_VERBOSE:
        print(f"{tag} PROMPT ({len(prompt)} chars):\n{prompt[:500]}{'...' if len(prompt) > 500 else ''}")
    else:
        print(f"{tag} PROMPT ({len(prompt)} chars)")

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
        result = data["choices"][0]["message"]["content"]

    if AI_LOG_VERBOSE:
        print(f"{tag} RESPONSE ({len(result)} chars):\n{result[:500]}{'...' if len(result) > 500 else ''}")
    else:
        print(f"{tag} RESPONSE ({len(result)} chars)")
    return result


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


async def sweep_stale_analyzing_rows():
    """
    Recover file_audit rows stuck in 'analyzing'. A row enters this state
    the moment /analyze claims it (see build_claim_file_audit_sql) and should
    leave it within minutes under normal discovery runtimes. If a row has
    sat in 'analyzing' longer than SESSION_TIMEOUT_SECONDS, the process that
    claimed it almost certainly crashed or was killed before finishing —
    the in-memory session is gone, so this is a pure SQL recovery, not a
    lookup against _sessions. Moves it to 'failed' so it surfaces through
    the existing file_audit alerts webhook instead of sitting invisible.
    """
    while True:
        await asyncio.sleep(60)
        try:
            await call_postgres(f"""
UPDATE file_audit
SET status = 'failed',
    notes  = COALESCE(notes || E'\\n', '') || 'Auto-failed: stuck in analyzing past {SESSION_TIMEOUT_SECONDS}s — discovery process likely crashed',
    updated_at = now()
WHERE status = 'analyzing'
  AND updated_at < now() - INTERVAL '{SESSION_TIMEOUT_SECONDS} seconds'
""".strip())
        except Exception:
            # Don't let a transient DB error kill the sweep loop itself
            pass
