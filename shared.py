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
from datetime import datetime, date, timedelta
from typing import Optional

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
SELECT id, filename, file_hash, minio_path, retailer, status, discovery_result, resolved_dates
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


def build_fetch_existing_patterns_for_dedup_sql() -> str:
    """
    ALL patterns regardless of status (active AND pending_review) - used
    only to check "has AI already been asked about a shape like this," never
    to trust an unapproved pattern for actual date computation. Without
    this, re-processing the same file (or a similar one) asks AI fresh and
    writes a fresh pending_review row every time. Includes status so
    callers can tell an active-backed match (safe to persist a computed
    date for ingestion to trust directly) from a pending one (computed for
    internal use only, not yet safe to hand to ingestion as fact).
    """
    return "SELECT retailer, pattern_regex, resolution_rule, format_description, status FROM date_format_patterns"


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

    Dispatches on the STRUCTURE of capture_groups (which keys are present),
    not on the AI-supplied "method" string. In production, a model
    correctly identified year/week capture groups but wrote method:
    "unknown" instead of the requested "iso_year_week" - the structure was
    right, the label wasn't. Requiring an exact method-string match would
    have silently refused to compute a date for a genuinely well-formed
    resolution, purely because of an inconsistent label. The "method"
    field is still recorded (useful for human review / audit trail) but is
    no longer load-bearing for the computation itself.

    Only implements structures Python actually knows how to compute. An
    unrecognized structure returns None deliberately - the caller (discovery,
    for column enumeration; ingestion, for date resolution) can still use
    the pattern MATCH itself for its own purposes even when no computed
    date is available yet - that's an honest, bounded gap rather than a
    wrong silent answer, and is closed by adding a new branch here, once,
    for both pipelines simultaneously.
    """
    groups = resolution_rule.get("capture_groups", {}) or {}
    try:
        if "year" in groups and "week" in groups:
            year = int(match.group(groups["year"]))
            week = int(match.group(groups["week"]))
            d = date.fromisocalendar(year, max(1, min(week, 53)), 6)  # Saturday, matching normalize_to_saturday
            return d.isoformat()
    except (KeyError, ValueError, IndexError, Exception):
        return None
    return None


# ─────────────────────────────────────────────
# DATE HEADER RESOLUTION (moved here from ingestion.py)
# ─────────────────────────────────────────────
# This whole algorithm used to live only in ingestion.py, called at
# ingestion time to figure out which date each column header represents.
# That meant ingestion was independently RE-DERIVING something discovery
# had already worked out (or should have) - and every time that derivation
# used a different piece of state than what discovery actually computed
# (a stale pattern cache, the wrong header row), it silently produced
# wrong or empty results with no error, because "figure out the date" and
# "use the date" were never actually the same, verified computation.
#
# Discovery now calls build_date_map() itself (see stage_date_config) and
# persists the COMPLETE result into date_config[sheet]["resolved_dates"] -
# every date column, not just ones from newly-discovered patterns.
# Ingestion no longer calls anything here at all; it only reads that
# persisted dict. This function stays here as the one place this logic is
# implemented, used by discovery to compute it once.

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
}


def extract_leading_month(val) -> Optional[int]:
    """
    Extract the tracking month from a date header — used to detect year
    rollovers via monotonic sequence. For date ranges we use the END month
    since that's what gets resolved as the actual date. For all other formats
    we use the leading/only month.
    """
    if isinstance(val, datetime):
        return val.month
    if isinstance(val, date):
        return val.month
    if not isinstance(val, str):
        return None
    s = val.strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}', s):
        try:
            return int(s[5:7])
        except (ValueError, IndexError):
            return None
    m = re.match(r'^(\d{1,2})/(\d{1,2})[-–](\d{1,2})/(\d{1,2})$', s)
    if m:
        return int(m.group(3))
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', s)
    if m:
        return int(m.group(1))
    m = re.match(r'^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+wk\s*\d+$', s, re.IGNORECASE)
    if m:
        return MONTH_MAP[m.group(1).lower()[:3]]
    return None


def resolve_date_header(val, year: int) -> Optional[date]:
    """
    Resolve a single date header value to a week-ending Saturday,
    given an already-determined year. No year logic here — year is
    passed in from build_date_map which owns that decision.
    """
    if isinstance(val, datetime):
        return normalize_to_saturday(val.date())
    if isinstance(val, date):
        return normalize_to_saturday(val)
    if not isinstance(val, str):
        return None
    s = val.strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}', s):
        try:
            return normalize_to_saturday(datetime.strptime(s[:10], "%Y-%m-%d").date())
        except (ValueError, OverflowError):
            return None
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', s)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), m.group(3)
        yyyy = int(yy) if len(yy) == 4 else (2000 + int(yy))
        try:
            return normalize_to_saturday(date(yyyy, mm, dd))
        except (ValueError, OverflowError):
            return None
    m = re.match(r'^(\d{1,2})/(\d{1,2})[-–](\d{1,2})/(\d{1,2})$', s)
    if m:
        end_month, end_day = int(m.group(3)), int(m.group(4))
        try:
            return date(year, end_month, end_day)
        except (ValueError, OverflowError):
            return None
    m = re.match(r'^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+wk\s*(\d+)$', s, re.IGNORECASE)
    if m:
        month_num = MONTH_MAP[m.group(1).lower()[:3]]
        week_num  = int(m.group(2))
        try:
            first_of_month = date(year, month_num, 1)
            approx = first_of_month + timedelta(days=(week_num - 1) * 7)
            return normalize_to_saturday(approx)
        except (ValueError, OverflowError):
            return None

    match = match_known_patterns(s)
    if match:
        m2 = re.match(match["pattern_regex"], s, re.IGNORECASE)
        if m2:
            computed = compute_date_from_match(m2, match.get("resolution_rule", {}))
            if computed:
                try:
                    return normalize_to_saturday(date.fromisoformat(computed))
                except (ValueError, OverflowError):
                    return None
    return None


def build_date_map(header_row: tuple, date_col_idxs: list, date_config: dict) -> dict:
    """
    Build col_idx -> week_ending date mapping for all date columns.

    Year assignment uses the monotonic-sequence rule:
    - Start from year_value (discovery's determination)
    - Extract the leading month from each header in sequence
    - When the month number drops (e.g. Dec→Jan, or Wk4→Wk1 across months),
      increment the current year
    - Assign the current year to each column before resolving its date

    For formats with year embedded (datetime objects, "01/04/25" strings,
    ISO strings, or a pattern-matched format whose capture groups carry
    their own year), the embedded year takes precedence and the monotonic
    rule is skipped for that column.
    """
    base_year     = date_config.get("year_value", date.today().year)
    current_year  = date_config.get("year_start", base_year)
    prev_month    = None
    date_map      = {}

    resolved_dates = {
        int(k): v for k, v in date_config.get("resolved_dates", {}).items()
    }

    for col_idx in date_col_idxs:
        if col_idx >= len(header_row):
            continue
        val = header_row[col_idx]
        if val is None:
            continue

        if col_idx in resolved_dates:
            try:
                date_map[col_idx] = date.fromisoformat(resolved_dates[col_idx])
            except (ValueError, TypeError):
                pass
            else:
                prev_month = extract_leading_month(val)
                continue

        pattern_match = match_known_patterns(val) if isinstance(val, str) else None
        pattern_groups = (pattern_match or {}).get("resolution_rule", {}).get("capture_groups", {}) or {}
        has_pattern_embedded_year = pattern_match is not None and "year" in pattern_groups
        has_embedded_year = (
            isinstance(val, (datetime, date)) or
            (isinstance(val, str) and (
                re.match(r'^\d{4}-\d{2}-\d{2}', val.strip()) or
                re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', val.strip())
            )) or
            has_pattern_embedded_year
        )
        if has_embedded_year:
            resolved = resolve_date_header(val, current_year)
            if resolved:
                date_map[col_idx] = resolved
                prev_month = extract_leading_month(val)
            continue

        leading_month = extract_leading_month(val)
        if leading_month is not None and prev_month is not None:
            if leading_month < prev_month:
                current_year += 1
        if leading_month is not None:
            prev_month = leading_month

        resolved = resolve_date_header(val, current_year)
        if resolved:
            date_map[col_idx] = resolved

    return date_map


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
