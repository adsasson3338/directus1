"""
discovery.py — Discovery pipeline: qualify, locate, identify, date, write audit.
"""
from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
import asyncio
import openpyxl
import base64
import hashlib
import io
import re
import uuid
import json
import time
from datetime import datetime, date, timedelta

from shared import (
    _sessions, _jobs,
    call_postgres, call_ai, fire_fetch_file_webhook,
    normalize_to_saturday,
    _validate_uuid, _sql_escape,
    build_fetch_audit_row_sql,
)

# ─────────────────────────────────────────────
# AI RESPONSE PARSER
# ─────────────────────────────────────────────

def parse_ai_response(text: str) -> str:
    """Strip thinking blocks and code fences from AI response before JSON parsing."""
    if not isinstance(text, str):
        return text
    # Strip <think>...</think> blocks (Qwen and other thinking models)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Strip ```json and ``` fences
    text = text.replace("```json", "").replace("```", "")
    return text.strip()


router = APIRouter()

# ─────────────────────────────────────────────
# CELL CLASSIFICATION
# ─────────────────────────────────────────────

DATE_RANGE_RE  = re.compile(r"^\d{1,2}/\d{1,2}[-–]\d{1,2}/\d{1,2}$")
FISCAL_WEEK_RE = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+wk\s*\d+$", re.IGNORECASE)
WK_NUMBER_RE   = re.compile(r"^wk\s*\d+$", re.IGNORECASE)
YEAR_RE        = re.compile(r"\b(20\d{2})\b")

def classify_cell(val) -> str:
    if val is None:               return "empty"
    if isinstance(val, bool):     return "bool"
    if isinstance(val, datetime): return "datetime"
    if isinstance(val, int):      return "integer"
    if isinstance(val, float):
        return "float_0_to_1" if 0 <= val <= 1 else "float_above_1"
    if isinstance(val, str):
        s = val.strip()
        if DATE_RANGE_RE.match(s):  return "date_range_string"
        if FISCAL_WEEK_RE.match(s): return "fiscal_week_label"
        if WK_NUMBER_RE.match(s):   return "week_number_label"
        return "string"
    return "unknown"

def is_date_like(val) -> bool:
    return classify_cell(val) in (
        "datetime", "date_range_string", "fiscal_week_label", "week_number_label"
    )



EMBEDDED_PATTERNS = [
    ("integer_dash_description",        re.compile(r'^(\d{4,10})\s*[-–]\s*(.{3,})$')),
    ("alphanumeric_space_description",  re.compile(r'^([A-Z0-9]{2,}-[A-Z0-9\-]{2,})\s+(.{3,})$', re.IGNORECASE)),
    ("description_space_alphanumeric",  re.compile(r'^(.{3,})\s{2,}([A-Z0-9]{2,}-[A-Z0-9\-]{2,})\s*$', re.IGNORECASE)),
    ("description_space_nodash_sku",    re.compile(r'^(.{5,})\s+([A-Z]{2,}\d{3,})\s*$', re.IGNORECASE)),
]

# ─────────────────────────────────────────────
# GRID DETECTION
# ─────────────────────────────────────────────

def find_date_axis(rows) -> dict | None:
    best = {"row": None, "count": 0, "cols": [], "samples": [], "format": None}
    for row_idx, row in enumerate(rows[:15]):
        date_cols = [(ci, v) for ci, v in enumerate(row) if is_date_like(v)]
        if len(date_cols) > best["count"]:
            formats = set(classify_cell(v) for _, v in date_cols)
            best = {
                "row": row_idx, "count": len(date_cols),
                "cols": [ci for ci, _ in date_cols],
                "samples": [str(v) for _, v in date_cols[:8]],
                "format": list(formats)[0] if len(formats) == 1 else "mixed",
            }
    if best["count"] < 2:
        return None

    cols = best["cols"]
    year_present = best["format"] == "datetime" or any(YEAR_RE.search(s) for s in best["samples"])

    interleaved = False
    if len(cols) >= 3:
        gaps = [cols[i+1] - cols[i] for i in range(len(cols)-1)]
        interleaved = len(set(gaps)) == 1 and gaps[0] == 2

    year_boundary_detected = False
    if best["format"] in ("date_range_string", "mixed"):
        months = set()
        for s in best["samples"]:
            m = re.match(r'^(\d{1,2})/', s.strip())
            if m:
                months.add(int(m.group(1)))
        if 12 in months and 1 in months:
            year_boundary_detected = True

    # Filter out inventory/summary columns that have date headers but are not sales
    # Check the row above the date axis for inventory-related labels
    INVENTORY_LABELS = {
        'inv', 'inventory', 'on order', 'onorder', 'dc inv', 'store inv',
        'total inv', 'on hand', 'onhand', 'stock', 'qty on hand',
        'remaining', 'order qty', 'open order'
    }
    if best["row"] > 0:
        label_row = rows[best["row"] - 1]
        filtered_cols = []
        filtered_samples = []
        for ci, sample in zip(cols, best["samples"]):
            label = str(label_row[ci]).strip().lower() if ci < len(label_row) and label_row[ci] else ""
            if any(inv in label for inv in INVENTORY_LABELS):
                continue  # exclude inventory columns from date axis
            filtered_cols.append(ci)
            filtered_samples.append(sample)
        cols          = filtered_cols
        best["samples"] = filtered_samples

    return {
        "row": best["row"], "col_count": len(cols), "cols": cols,
        "sample_values": best["samples"], "format": best["format"],
        "year_present": year_present, "interleaved_empty_cols": interleaved,
        "year_boundary_detected": year_boundary_detected,
    }


def find_data_start_row(rows, date_axis_row: int, date_cols: list) -> int:
    for row_idx in range(date_axis_row + 1, min(date_axis_row + 10, len(rows))):
        row = rows[row_idx]
        vals = [row[dc] for dc in date_cols if dc < len(row) and row[dc] is not None]
        numeric = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if numeric:
            return row_idx
    return date_axis_row + 1


def detect_embedded_sku(rows, date_cols: list, data_start_row: int) -> list:
    if not date_cols:
        return []
    left_boundary = min(date_cols)
    embedded_candidates = []

    for col_idx in range(left_boundary):
        col_vals = [
            str(rows[r][col_idx]).strip()
            for r in range(data_start_row, min(data_start_row + 30, len(rows)))
            if col_idx < len(rows[r]) and rows[r][col_idx] is not None
            and isinstance(rows[r][col_idx], str)
        ]
        if len(col_vals) < 3:
            continue

        col_matches = []
        seen = set()
        for pattern_name, pattern_re in EMBEDDED_PATTERNS:
            for val in col_vals:
                if val not in seen:
                    m = pattern_re.match(val)
                    if m:
                        seen.add(val)
                        # description_space_* patterns have description in group(1), SKU in group(2)
                        if pattern_name.startswith("description_space"):
                            sku, desc = m.group(2).strip(), m.group(1).strip()
                        else:
                            sku, desc = m.group(1).strip(), m.group(2).strip()
                        col_matches.append({
                            "raw": val, "sku": sku,
                            "description": desc, "pattern": pattern_name,
                        })
        if col_matches:
            embedded_candidates.append({
                "col": col_idx, "total_values": len(col_vals),
                "matched_values": len(col_matches), "extractions": col_matches,
            })
    return embedded_candidates



# ─────────────────────────────────────────────
# QUALIFY SIGNALS
# ─────────────────────────────────────────────

def extract_qualify_signals(rows: list, sheet_name: str, filename: str) -> dict:
    integer_count = float_above_count = float_0_to_1_count = 0
    crosshair_sample = []

    date_axis = find_date_axis(rows)
    if date_axis:
        date_cols  = date_axis["cols"][:8]
        data_start = find_data_start_row(rows, date_axis["row"], date_axis["cols"])
        for row in rows[data_start:data_start + 50]:
            for dc in date_cols:
                if dc >= len(row) or row[dc] is None:
                    continue
                v = row[dc]
                if isinstance(v, bool):
                    continue
                if isinstance(v, int):
                    integer_count += 1
                    if len(crosshair_sample) < 40:
                        crosshair_sample.append(v)
                elif isinstance(v, float) and v > 1:
                    float_above_count += 1
                    if len(crosshair_sample) < 40:
                        crosshair_sample.append(v)
                elif isinstance(v, float) and 0 < v <= 1:
                    float_0_to_1_count += 1
                    if len(crosshair_sample) < 40:
                        crosshair_sample.append(v)
        total = integer_count + float_above_count + float_0_to_1_count
        if total == 0:                            dominant_type = None
        elif float_above_count / total > 0.7:     dominant_type = "float_above_1"
        elif float_0_to_1_count / total > 0.7:   dominant_type = "float_0_to_1"
        elif integer_count / total > 0.7:         dominant_type = "integer"
        else:                                     dominant_type = "mixed"
    else:
        for row in rows[:50]:
            for v in row:
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if len(crosshair_sample) < 40:
                        crosshair_sample.append(v)
            if len(crosshair_sample) >= 40:
                break
        dominant_type = None

    column_labels = set()
    for row in rows[:6]:
        for val in row:
            if isinstance(val, str) and val.strip() and len(val.strip()) < 60:
                column_labels.add(val.strip())

    inline_strings = set()
    for row in rows[6:20]:
        for val in row:
            if isinstance(val, str) and val.strip() and len(val.strip()) < 80:
                inline_strings.add(val.strip())

    return {
        "sheet_name":       sheet_name,
        "filename":         filename,
        "dominant_type":    dominant_type,
        "crosshair_sample": crosshair_sample,
        "column_labels":    sorted(column_labels, key=len)[:10],
        "inline_strings":   sorted(inline_strings, key=len)[:10],
        "date_col_count":   len(date_axis["cols"]) if date_axis else 0,
    }


def build_qualify_prompt(signals: dict) -> str:
    return f"""You are the gatekeeper of a retail sales data ingestion pipeline. A sheet has arrived and you must determine if it should be disqualified.

A sheet should be disqualified if it does NOT contain unit sales data. Unit sales data has integer values at the intersection of product identifiers and date columns.

Disqualify if any of the following are true:
- Crosshair values are decimals above 1 — dollar revenue
- Crosshair values are between 0 and 1 — percentage metrics
- Column labels contain $$$ or $$ — dollar sheet
- Sheet name contains CFP, Forecast, FCST, Projection, Order
- Vocabulary contains forecast or projection language

EVIDENCE:
Sheet name: {signals["sheet_name"]}
Filename: {signals["filename"]}
Detected date columns: {signals.get("date_col_count", 0)}
Crosshair sample values: {signals["crosshair_sample"]}
Dominant crosshair type: {signals["dominant_type"]}
Column labels: {signals["column_labels"]}
Inline strings: {signals["inline_strings"]}

Respond with JSON only:
{{"disqualified": true or false, "reason": "one sentence explanation"}}"""


# ─────────────────────────────────────────────
# POSTGRES SQL BUILDER
# ─────────────────────────────────────────────

def build_sku_lookup_sql(candidates: list) -> tuple[str, list]:
    """
    Build case-insensitive exact match query against inventory_view.
    Values embedded inline — n8n Postgres node does not support $1 params.
    Returns (sql, []) — empty params list.
    """
    # Safely quote each candidate for inline SQL
    quoted = ", ".join(
        "'" + str(c).replace("'", "''") + "'"
        for c in candidates
        if c and str(c).strip()
    )
    if not quoted:
        quoted = "''"

    sql = f"""SELECT inventory_sku, base_model, base_variant, description, upc
FROM inventory_view
WHERE UPPER(inventory_sku) = ANY(ARRAY[{quoted}]::text[])
   OR UPPER(base_variant)  = ANY(ARRAY[{quoted}]::text[])
   OR UPPER(base_model)    = ANY(ARRAY[{quoted}]::text[])"""

    return sql, []


# ─────────────────────────────────────────────
# COLUMN CLASSIFY PROMPT
# ─────────────────────────────────────────────


def build_date_prompt(date_axis: dict, year_anchors: list, sheet_name: str,
                       filename: str = "", cross_sheet_anchors: list = None) -> str:
    cross_context = ""
    if cross_sheet_anchors:
        cross_context = f"\nYear anchors from other sheets in same file: {cross_sheet_anchors}"

    return f"""You are configuring the date settings for a retail sales sheet.

Determine the year settings from the evidence below.
The file was received as: {filename}

Sheet: {sheet_name}
Date axis format: {date_axis["format"]}
Year present in dates: {date_axis["year_present"]}
Year boundary detected: {date_axis.get("year_boundary_detected", False)}
Sample date values: {date_axis["sample_values"]}
Year anchors in this sheet: {year_anchors}{cross_context}

Rules:
- year_value is the primary/later year (e.g. the year the file was sent)
- year_start is the year the FIRST date column belongs to
- If dates span December and January: December belongs to the EARLIER year, January to the LATER year
- If year_boundary_detected is true, year_start will be year_value - 1
- If all dates are in one year, year_start equals year_value
- All dates will be normalized to week-ending Saturday

Example for a March 2026 file containing Dec 2025 through Mar 2026 data:
year_value = 2026, year_start = 2025, year_boundary_detected = true

Respond with JSON only:
{{"year_value": 2026, "year_start": 2025, "year_boundary_detected": true, "year_inference_strategy": "one sentence", "week_convention": "what convention the source uses"}}"""



# ─────────────────────────────────────────────
# RETAILER IDENTIFICATION SQL
# ─────────────────────────────────────────────



def build_dedup_check_sql(file_hash: str) -> str:
    """Check if this file has already been seen — any status means duplicate."""
    return f"""
SELECT id, status, filename
FROM file_audit
WHERE file_hash = '{file_hash}'
LIMIT 1
""".strip()

def build_retailer_identify_sql(retailer_sku_candidates: list) -> str:
    """
    Query retailer_sku_map to identify retailer from SKU candidates.
    Returns retailer and match count — 3+ matches confirms identity.
    """
    quoted = ", ".join(
        "'" + str(c).replace("'", "''") + "'"
        for c in retailer_sku_candidates
        if c and str(c).strip()
    )
    if not quoted:
        quoted = "''"

    return f"""
SELECT retailer, COUNT(*) as matches
FROM retailer_sku_map
WHERE UPPER(retailer_sku) = ANY(ARRAY[{quoted}]::text[])
  AND active = true
GROUP BY retailer
ORDER BY matches DESC
LIMIT 1
""".strip()


def sql_escape(v) -> str:
    """Escape a value for safe inline SQL embedding."""
    return str(v).replace("'", "''")


def json_safe(v) -> str:
    """Serialize to JSON with unicode preserved, then SQL-escape."""
    return sql_escape(json.dumps(v, ensure_ascii=False))


def build_insert_retailer_config_sql(retailer: str) -> str:
    """
    Insert a pending_review config row for a newly identified retailer.
    Only fires once — skipped if retailer already exists.
    """
    safe_retailer = _sql_escape(retailer) if hasattr(retailer, "__class__") else str(retailer).replace("'", "''")
    return f"""
INSERT INTO retailer_configs (retailer, status, file_set_size)
SELECT '{safe_retailer}', 'pending_review', 1
WHERE NOT EXISTS (
    SELECT 1 FROM retailer_configs
    WHERE UPPER(retailer) = UPPER('{safe_retailer}')
)
""".strip()


def build_retailer_identify_prompt(filename: str, sheets: list, header_strings: list) -> str:
    """
    Build a prompt to identify retailer from file/sheet clues when Postgres has no match.
    """
    return f"""You are identifying which retailer sent a sales file to a supplier.

Known retailers: Walgreens, Target, Staples, Walmart, CVS, Rite Aid, Best Buy, Amazon, Costco, BJ's, Sam's Club.

CLUES:
Filename: {filename}
Sheet names: {sheets}
Header strings found in sheets: {header_strings[:20]}

Only identify the retailer if the clues EXPLICITLY mention the retailer name or a well-known retailer-specific identifier (e.g. "Walgreens", "WIC#", "DPCI", "Staples").

DO NOT GUESS. If the retailer name is not explicitly present in the filename, sheet names, or headers, return null.
A generic filename like "D56_FCSTs_2026.xlsx" with no retailer name is NOT sufficient — return null.

Respond with JSON only:
{{"retailer": "retailer name or null", "confidence": "high/medium/low", "reason": "one sentence"}}"""


def build_query_retailer_config_sql(retailer: str) -> str:
    """Query active retailer config to get file_set_size."""
    retailer_safe = retailer.replace("'", "''")
    return f"""
SELECT file_set_size, id
FROM retailer_configs
WHERE UPPER(retailer) = UPPER('{retailer_safe}')
  AND status = 'active'
LIMIT 1
""".strip()


def build_file_set_key(retailer: str, date_config: dict, grid: dict = None) -> str:
    """
    Build a file set key from retailer and the latest week-ending date in the file.
    Uses the most recent parseable date from the grid date_axis sample_values.
    Format: RETAILER_YYYY-MM-DD (week-ending Saturday of latest data week)
    Falls back to RETAILER_YYYY-WNN only if no date can be parsed.
    """
    from datetime import date as _date, timedelta
    import re

    retailer_clean = re.sub(r"[^A-Z0-9]", "_", retailer.upper()).strip("_")

    # Collect year_value and year_boundary from date_config
    year_value    = None
    year_boundary = False
    for sheet_cfg in date_config.values():
        if sheet_cfg.get("year_value"):
            year_value    = sheet_cfg["year_value"]
            year_boundary = sheet_cfg.get("year_boundary_detected", False)
            break
    if not year_value:
        year_value = _date.today().year

    # Try to extract a real week-ending date from grid sample_values
    latest_date = None
    if grid:
        for sheet_grid in grid.values():
            date_axis = sheet_grid.get("date_axis", {})
            samples   = date_axis.get("sample_values", [])
            fmt       = date_axis.get("format", "")

            for sample in samples:
                s = str(sample).strip()
                parsed = None

                # MM/DD/YY or MM/DD/YYYY
                m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s)
                if m:
                    mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    yr = 2000 + yr if yr < 100 else yr
                    try:
                        parsed = _date(yr, mo, dy)
                    except ValueError:
                        pass

                # MM/DD-MM/DD (date range — use end date)
                if not parsed:
                    m = re.match(r"^(\d{1,2})/(\d{1,2})[-–](\d{1,2})/(\d{1,2})$", s)
                    if m:
                        end_mo, end_dy = int(m.group(3)), int(m.group(4))
                        start_mo       = int(m.group(1))
                        end_yr         = year_value
                        if year_boundary and end_mo == 12:
                            end_yr = year_value - 1
                        elif year_boundary and start_mo == 12 and end_mo == 1:
                            end_yr = year_value + 1
                        try:
                            parsed = _date(end_yr, end_mo, end_dy)
                        except ValueError:
                            pass

                # Mon Wk N — approximate to Saturday
                if not parsed:
                    MONTH_MAP = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                                 "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
                    m = re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+wk\s*(\d+)$",
                                 s, re.IGNORECASE)
                    if m:
                        mon_num  = MONTH_MAP[m.group(1).lower()[:3]]
                        wk_num   = int(m.group(2))
                        eff_year = (year_value + 1) if (year_boundary and mon_num == 1) else year_value
                        try:
                            first = _date(eff_year, mon_num, 1)
                            approx = first + timedelta(days=(wk_num - 1) * 7)
                            # normalize to Saturday
                            parsed = approx + timedelta(days=(5 - approx.weekday()) % 7)
                        except ValueError:
                            pass

                if parsed:
                    if latest_date is None or parsed > latest_date:
                        latest_date = parsed

    if latest_date:
        return f"{retailer_clean}_{latest_date.isoformat()}"

    # Fallback — no parseable date found
    week_num = _date.today().isocalendar()[1]
    return f"{retailer_clean}_{year_value}-W{week_num:02d}"


def build_claim_file_audit_sql(file_audit_id: str) -> str:
    """
    Atomically flip status received -> analyzing, conditioned on the row
    still being 'received'. Returns the updated id if this call won the
    race, or zero rows if another concurrent poll already claimed it.
    Prevents the same file_audit row from being picked up twice by
    overlapping polling runs while discovery is still in progress.
    """
    return f"""
UPDATE file_audit
SET status = 'analyzing', updated_at = now()
WHERE id = '{file_audit_id}'
  AND status = 'received'
RETURNING id
""".strip()


def build_update_file_audit_full_sql(file_audit_id: str, discovery_result: dict,
                                      retailer: str | None, status: str,
                                      file_set_key: str | None,
                                      file_hash: str | None = None,
                                      filename: str | None = None) -> str:
    """Update file_audit — writes discovery_result plus dedicated columns for easy querying."""
    result_b64 = base64.b64encode(json.dumps(discovery_result, ensure_ascii=False).encode()).decode()

    # Extract the three dedicated columns directly
    qs_b64 = base64.b64encode(json.dumps(discovery_result.get("qualified_sheets", []), ensure_ascii=False).encode()).decode()
    cm_b64 = base64.b64encode(json.dumps(discovery_result.get("column_mapping",   {}), ensure_ascii=False).encode()).decode()
    dc_b64 = base64.b64encode(json.dumps(discovery_result.get("date_config",      {}), ensure_ascii=False).encode()).decode()

    retailer_val = f"'{sql_escape(retailer)}'" if retailer else "NULL"
    key_val      = f"'{sql_escape(file_set_key)}'" if file_set_key else "NULL"
    hash_val     = f"'{file_hash}'" if file_hash else "NULL"
    fname_val    = f"'{sql_escape(filename)}'" if filename else "NULL"

    return f"""
UPDATE file_audit
SET discovery_result = convert_from(decode('{result_b64}', 'base64'), 'UTF8')::jsonb,
    qualified_sheets = convert_from(decode('{qs_b64}', 'base64'), 'UTF8')::jsonb,
    column_mapping   = convert_from(decode('{cm_b64}', 'base64'), 'UTF8')::jsonb,
    date_config      = convert_from(decode('{dc_b64}', 'base64'), 'UTF8')::jsonb,
    retailer         = {retailer_val},
    status           = '{status}',
    file_set_key     = {key_val},
    file_hash        = {hash_val},
    filename         = {fname_val},
    updated_at       = now()
WHERE id = '{file_audit_id}'
""".strip()


def extract_column_schema(ws, sheet_name: str) -> dict:
    """
    Extract raw column schema from worksheet.
    Detects data block start, header rows, merged cells, borders.
    Returns per-column: header stack, borders, data stats.
    """
    max_col = ws.max_column
    max_row  = ws.max_row

    # Build merged cell map
    merge_map = {}
    for merge in ws.merged_cells.ranges:
        val = ws.cell(merge.min_row, merge.min_col).value
        for row in range(merge.min_row, merge.max_row + 1):
            for col in range(merge.min_col, merge.max_col + 1):
                merge_map[(row, col)] = {
                    "value": val,
                    "range": str(merge),
                    "is_top_left": (row == merge.min_row and col == merge.min_col)
                }

    def get_cell_value(row, col):
        if (row, col) in merge_map:
            return merge_map[(row, col)]["value"]
        return ws.cell(row, col).value

    # Find data block start — first row with >30% numeric values
    data_start_row = None
    for row_idx in range(1, min(30, max_row + 1)):
        values = [ws.cell(row_idx, c).value for c in range(1, max_col + 1)]
        numeric = sum(1 for v in values if isinstance(v, (int, float)) and not isinstance(v, bool))
        non_null = sum(1 for v in values if v is not None)
        if non_null > 0 and numeric / max(non_null, 1) > 0.3:
            data_start_row = row_idx
            break

    if not data_start_row:
        data_start_row = 2

    header_rows = list(range(1, data_start_row))

    columns = []
    for col_idx in range(1, max_col + 1):
        # Collect header stack
        header_stack = []
        for row_idx in header_rows:
            val = get_cell_value(row_idx, col_idx)
            if val is not None and str(val).strip():
                if isinstance(val, datetime):
                    val = val.strftime('%m/%d/%y')
                is_merged   = (row_idx, col_idx) in merge_map
                merge_parent = merge_map.get((row_idx, col_idx), {}).get("is_top_left", False)
                header_stack.append({
                    "row":          row_idx,
                    "value":        str(val).strip(),
                    "merged":       is_merged,
                    "merge_parent": merge_parent,
                })

        # Border info from last header row
        last_hrow = header_rows[-1] if header_rows else 1
        cell = ws.cell(last_hrow, col_idx)
        borders = {
            "left":   cell.border.left.style if cell.border.left else None,
            "right":  cell.border.right.style if cell.border.right else None,
            "top":    cell.border.top.style if cell.border.top else None,
            "bottom": cell.border.bottom.style if cell.border.bottom else None,
        }

        # Data stats
        data_values = [
            ws.cell(r, col_idx).value
            for r in range(data_start_row, min(data_start_row + 100, max_row + 1))
            if ws.cell(r, col_idx).value is not None
        ]
        total    = len(data_values)
        zeros    = sum(1 for v in data_values if v == 0 or v == 0.0)
        non_zero = [v for v in data_values if v and v != 0]
        pct_zero = round(zeros / total, 2) if total > 0 else None
        sample   = [str(v)[:20] for v in non_zero[:5]]

        types = set()
        for v in data_values[:50]:
            if isinstance(v, bool):     types.add("bool")
            elif isinstance(v, int):    types.add("integer")
            elif isinstance(v, float):  types.add("float")
            elif isinstance(v, str):    types.add("string")
            elif isinstance(v, datetime): types.add("datetime")

        columns.append({
            "col":              col_idx,
            "header_stack":     header_stack,
            "borders":          borders,
            "data_types":       sorted(types),
            "sample_non_zero":  sample,
            "pct_zero":         pct_zero,
            "total_data_rows":  total,
        })

    return {
        "sheet_name":     sheet_name,
        "data_start_row": data_start_row,
        "header_rows":    header_rows,
        "columns":        [c for c in columns if c["header_stack"] or c["sample_non_zero"]],
        "merged_ranges":  [str(m) for m in ws.merged_cells.ranges],
    }


def build_date_schema_prompt(schema: dict, filename: str) -> str:
    """
    Pass 1 — AI identifies the sales date column pattern.
    Python then enumerates all matching columns mechanically.
    """
    header_rows = schema.get("header_rows", [])

    # Build compact header grid showing all columns
    header_grid = []
    for row_num in header_rows:
        row_cells = []
        for col in schema.get("columns", []):
            val = ""
            for h in col.get("header_stack", []):
                if h["row"] == row_num:  # both are 1-based
                    val = h["value"][:15]
                    break
            if val:
                row_cells.append(f"c{col['col']}={repr(val)}")
        if row_cells:
            header_grid.append(f"Row {row_num}: {', '.join(row_cells[:50])}")

    grid_text = "\n".join(header_grid)

    return f"""You are analyzing the header structure of a retail sales spreadsheet to identify the sales date column pattern.

File: {filename}
Sheet: {schema["sheet_name"]}

HEADER ROWS (compact — showing only non-empty cells):
{grid_text}

Your task: Identify the pattern that defines weekly sales date columns.

Sales date columns are a consecutive block with:
- Weekly date values in one row (e.g. "01/03/26", "12/21-12/27", "Feb Wk 1")
- Possibly a section label in the row above (e.g. "Sales", "UNITS") — or nothing above them
- The block ENDS when the section label changes (e.g. "INV", "Total", "On Order") or a non-date value appears

Identify:
1. date_axis_row: row number (1-based) of the row containing the ACTUAL DATE VALUES like "01/03/26", "12/21-12/27", "Feb Wk 1" — NOT the column label row with text like "SKU Number" or "Business Unit"
2. section_label_row: row number (1-based) containing section group labels like "Sales" or "INV" ABOVE the date row (null if none)
3. sales_section_label: the exact label above the SALES date columns (e.g. "Sales", null if no label)
4. stop_labels: exact labels that signal the END of the sales block (e.g. ["INV", "TOTAL", "On Order"])
5. first_sales_col: column number (1-based) of the FIRST column containing a sales date value
6. date_format: pattern e.g. "MM/DD/YY", "MM/DD-MM/DD", "Mon Wk N"
7. data_start_row: first row (1-based) with actual product/SKU data
8. year_present: true only if a year is embedded in the date values themselves
9. year_boundary: true if dates span December and January

IMPORTANT: date_axis_row is the row with actual date/week values, not text labels.

Respond with JSON only:
{{
  "date_axis_row": 5,
  "section_label_row": 4,
  "sales_section_label": "Sales",
  "stop_labels": ["INV", "TOTAL", "On Order"],
  "first_sales_col": 6,
  "date_format": "MM/DD-MM/DD",
  "data_start_row": 7,
  "year_present": false,
  "year_boundary": true
}}"""


def detect_date_axis_row(schema: dict) -> int:
    """Detect the row containing the most date-like values. Returns 1-based row number."""
    row_date_counts = {}
    for col in schema.get("columns", []):
        for h in col.get("header_stack", []):
            row = h["row"]  # already 1-based
            val = str(h.get("value", "")).strip()
            if (re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", val) or
                re.match(r"^\d{1,2}/\d{1,2}[-–]\d{1,2}/\d{1,2}$", val) or
                re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+wk\s*\d+$", val, re.IGNORECASE) or
                re.match(r"^wk\s*\d+$", val, re.IGNORECASE)):
                row_date_counts[row] = row_date_counts.get(row, 0) + 1
    if not row_date_counts:
        return 1
    return max(row_date_counts, key=row_date_counts.get)


def find_sales_cols_from_schema(schema: dict, date_schema: dict) -> list:
    """
    Python enumerates sales date columns using the pattern identified by AI.
    Scans columns from first_sales_col, collecting those that match the date pattern.
    Stops when section label changes or date pattern breaks.
    """
    # Detect date_axis_row from schema directly — more reliable than AI
    date_axis_row    = detect_date_axis_row(schema)        # 1-based
    section_label_row = date_schema.get("section_label_row")      # 1-based or None
    sales_label      = (date_schema.get("sales_section_label") or "").strip().upper()
    stop_labels      = [s.strip().upper() for s in date_schema.get("stop_labels", [])]
    first_sales_col  = date_schema.get("first_sales_col", 1)      # 1-based
    date_format      = date_schema.get("date_format", "")

    # Build col -> header values map
    col_headers = {}
    for col in schema.get("columns", []):
        col_num = col["col"]
        headers = {h["row"]: h["value"] for h in col.get("header_stack", [])}  # already 1-based
        col_headers[col_num] = headers

    # Date matching patterns
    DATE_PATTERNS = [
        re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}$'),           # MM/DD/YY or MM/DD/YYYY
        re.compile(r'^\d{1,2}/\d{1,2}[-–]\d{1,2}/\d{1,2}$'), # MM/DD-MM/DD
        re.compile(r'^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+wk\s*\d+$', re.IGNORECASE),
        re.compile(r'^wk\s*\d+$', re.IGNORECASE),
    ]

    def is_date_value(val: str) -> bool:
        return any(p.match(val.strip()) for p in DATE_PATTERNS)

    def get_section_label(col_num: int) -> str:
        if not section_label_row:
            return ""
        return col_headers.get(col_num, {}).get(section_label_row, "").strip().upper()

    def get_date_value(col_num: int) -> str:
        return col_headers.get(col_num, {}).get(date_axis_row, "").strip()

    sales_cols = []
    all_col_nums = sorted(col_headers.keys())

    in_sales_block = False
    for col_num in all_col_nums:
        if col_num < first_sales_col:
            continue

        date_val    = get_date_value(col_num)
        sec_label   = get_section_label(col_num)

        # Check if section label signals end of sales block
        if sec_label and stop_labels and sec_label in stop_labels:
            break  # hit a stop label — sales block ended

        # Check if this column has a date value in the date axis row
        if date_val and is_date_value(date_val):
            # Verify section label matches (if sales has a label)
            if sales_label and sec_label and sec_label != sales_label:
                break  # section changed
            sales_cols.append(col_num)
            in_sales_block = True
        elif in_sales_block:
            # Was in sales block — if no date value and no section label, skip (empty col)
            if not date_val and not sec_label:
                continue
            # Otherwise the block has ended
            break

    return sales_cols


def build_column_classify_prompt(schema: dict, filename: str,
                                  date_cols: list, postgres_matched: set) -> str:
    """
    Pass 2 — AI classifies non-date columns only.
    Small focused prompt — only the product/inventory/metadata columns.
    """
    date_col_set = set(date_cols)
    non_date_cols = [
        c for c in schema.get("columns", [])
        if c["col"] not in date_col_set
        and (c.get("sample_non_zero") or c.get("header_stack"))
    ]

    # Annotate with postgres match info
    for col in non_date_cols:
        col["postgres_matched"] = any(
            str(v).upper() in postgres_matched
            for v in col.get("sample_non_zero", [])
        )
        col["embedded_postgres_matched"] = any(
            ext.get("sku", "").upper() in postgres_matched
            for ext in col.get("embedded_sku_extractions", [])
        )

    cols_json = json.dumps(non_date_cols, indent=2)

    return f"""You are classifying the non-sales columns of a retail sales spreadsheet.

File: {filename}
Sheet: {schema["sheet_name"]}

The sales date columns have already been identified. Classify only the columns below.

Each column has:
- header_stack: header values with row numbers (top-down)
- data_types: Python types found in data rows
- sample_non_zero: up to 5 non-zero sample values
- pct_zero: fraction of data rows that are zero/null
- postgres_matched: true if sample values matched the supplier inventory database
- embedded_sku_extractions: supplier SKUs extracted from text values
- embedded_postgres_matched: true if extracted SKUs matched the supplier database

Classify each column as one of:
- retailer_sku: retailer's product identifier (WIC#, DPCI, SKU Number, Item# etc)
- supplier_sku: supplier's internal part number or style code
- description: product name/description text (may contain embedded supplier SKU)
- cost: unit cost or wholesale price
- retail_price: retail selling price
- inventory: the single best current total on-hand quantity to ingest — pick exactly one per sheet
- open_order: open purchase order quantity (even if currently zero/empty)
- other: everything else — DC/store inventory sub-components, prior snapshots, YTD totals, percentages, business unit codes, store counts, etc

For inventory: if a sheet has sub-components (DC Inv, Store Inv) alongside a total (Total Inv), classify only the total as inventory. If snapshots are dated, use the most recent total. Only one column per sheet should be classified as inventory.
- postgres_matched strongly suggests retailer_sku or supplier_sku
- embedded_postgres_matched means has_embedded_supplier_sku = true

COLUMNS:
{cols_json}

Respond with JSON only:
{{
  "columns": [
    {{"col": 1, "classification": "retailer_sku", "confidence": "high", "reason": "one sentence", "has_embedded_supplier_sku": false}},
    ...
  ]
}}"""

# ─────────────────────────────────────────────
# FILE TYPE DETECTION
# ─────────────────────────────────────────────

# Keywords that identify known non-sales file types.
# Maps keyword (lowercase, found in filename or sheet names) → audit status
KNOWN_FILE_TYPES = {
    "on hand inventory alpha": "inventory_report",  # primary match — most specific first
    "on hand":                 "inventory_report",  # fallback
}

def detect_known_file_type(filename: str, sheet_names: list) -> str | None:
    """
    Check filename and sheet names against known non-sales file type keywords.
    Returns the audit status to assign, or None if unrecognized.
    """
    haystack = (filename + " " + " ".join(sheet_names)).lower()
    for keyword, status in KNOWN_FILE_TYPES.items():
        if keyword in haystack:
            return status
    return None


# ─────────────────────────────────────────────
# PIPELINE STAGES
# ─────────────────────────────────────────────

async def stage_qualify(session_id: str):
    """Stage 1 — qualify each sheet."""
    session = _sessions[session_id]
    session["stage"]  = "qualifying"
    session["status"] = "running"

    for sheet_name, rows in session["_sheets"].items():
        signals = extract_qualify_signals(rows, sheet_name, session["filename"])

        # Deterministic fast paths
        if signals["dominant_type"] == "float_above_1":
            session["qualify_results"][sheet_name] = {
                "disqualified": True,
                "reason": "Crosshair values predominantly decimals above 1 — dollar revenue",
                "source": "python",
            }
            continue
        if signals["dominant_type"] == "float_0_to_1":
            session["qualify_results"][sheet_name] = {
                "disqualified": True,
                "reason": "Crosshair values predominantly between 0 and 1 — percentage metrics",
                "source": "python",
            }
            continue

        # AI call — direct, no webhook
        try:
            text   = await call_ai(build_qualify_prompt(signals), label="qualify")
            clean  = parse_ai_response(text)
            verdict = json.loads(clean)
        except (json.JSONDecodeError, ValueError) as e:
            verdict = {"disqualified": None, "reason": f"Parse error: {e}"}
        except Exception as e:
            verdict = {"disqualified": None, "reason": f"AI error: {e}"}
        verdict["source"] = "ai"
        session["qualify_results"][sheet_name] = verdict

    await advance_from_qualify(session_id)


async def advance_from_qualify(session_id: str):
    """After all qualify jobs done — filter qualified sheets, advance to grid location."""
    session = _sessions[session_id]
    results = session["qualify_results"]

    qualified = [
        name for name, r in results.items()
        if not r.get("disqualified")
    ]

    session["qualified_sheets"] = qualified

    if not qualified:
        file_audit_id = session.get("file_audit_id")
        # Check if this is a known non-sales file type before marking rejected
        known_type = detect_known_file_type(
            session.get("filename", ""),
            list(session.get("_sheets", {}).keys())
        )
        audit_status = known_type or "rejected"
        if file_audit_id:
            try:
                sql = build_update_file_audit_full_sql(
                    file_audit_id,
                    {"status": "no_sales_data", "qualify_results": results},
                    None, audit_status, None,
                    file_hash=session.get("file_hash"),
                    filename=session.get("filename"),
                )
                await call_postgres(sql)
            except Exception as e:
                session.setdefault("errors", []).append(f"Failed to write {audit_status} status: {e}")
        session["stage"]  = "complete"
        session["status"] = "complete"
        session["result"] = {"status": audit_status, "qualify_results": results}
        return

    await stage_locate_grid(session_id)


async def stage_locate_grid(session_id: str):
    """
    Stage 2 — pure Python schema extraction.
    Opens workbook with full fidelity (merges + borders).
    Extracts per-column schema for all qualified sheets.
    Then advances to Postgres SKU lookup.
    """
    session  = _sessions[session_id]
    session["stage"]  = "locating"
    session["status"] = "running"

    # Need full workbook (not read-only) for merge + border detection
    wb_full = openpyxl.load_workbook(io.BytesIO(session["_raw_data"]), data_only=True)
    session["_schemas"] = {}

    for sheet_name in session["qualified_sheets"]:
        if sheet_name not in wb_full.sheetnames:
            session["grid"][sheet_name] = {"error": "Sheet not found in workbook"}
            continue
        ws     = wb_full[sheet_name]
        schema = extract_column_schema(ws, sheet_name)

        # Embedded SKU detection runs after schema_classify when we know sales_cols
        schema["embedded_sku"] = []

        session["_schemas"][sheet_name] = schema

    await stage_postgres_sku_lookup(session_id)





async def stage_postgres_sku_lookup(session_id: str):
    """Stage 2b — Postgres SKU lookup on candidate column values from all sheets."""
    session = _sessions[session_id]
    session["stage"]  = "identifying"
    session["status"] = "running"

    col_candidates = {}
    for sheet_name in session["qualified_sheets"]:
        schema     = session.get("_schemas", {}).get(sheet_name, {})
        rows       = session["_sheets"].get(sheet_name, [])
        data_start = schema.get("data_start_row", 1) - 1

        for col in schema.get("columns", []):
            for val in col.get("sample_non_zero", []):
                s = str(val).strip()
                if s:
                    col_candidates.setdefault(s, set()).add(col["col"])

            if "string" in col.get("data_types", []):
                col_0    = col["col"] - 1
                str_vals = [
                    str(rows[r][col_0]).strip()
                    for r in range(data_start, min(data_start + 30, len(rows)))
                    if col_0 < len(rows[r]) and isinstance(rows[r][col_0], str)
                    and rows[r][col_0].strip()
                ]
                for val in str_vals:
                    for _, pattern_re in EMBEDDED_PATTERNS:
                        m = pattern_re.match(val)
                        if m:
                            sku = m.group(2).strip() if pattern_re.pattern.startswith(r'^(.{3') else m.group(1).strip()
                            if sku:
                                col_candidates.setdefault(sku, set()).add(col["col"])
                            break

    session["_col_candidates"] = {k: list(v) for k, v in col_candidates.items()}

    try:
        sql, _ = build_sku_lookup_sql(sorted(col_candidates.keys()))
        matches = await call_postgres(sql)
        session["postgres_results"] = {"matches": matches}
    except Exception as e:
        session["postgres_results"] = {"matches": [], "error": str(e)}

    await stage_schema_classify(session_id)


async def stage_schema_classify(session_id: str):
    """Stage 2c — Pass 1: AI identifies date column pattern per sheet."""
    session = _sessions[session_id]
    session["stage"]  = "locating"
    session["status"] = "running"

    pg_matches     = session.get("postgres_results", {}).get("matches", [])
    matched_values = set()
    for row in pg_matches:
        for field in ("inventory_sku", "base_variant", "base_model"):
            v = row.get(field, "")
            if v:
                matched_values.add(v.upper())
    session["_matched_values"] = matched_values

    for sheet_name in session["qualified_sheets"]:
        schema = session.get("_schemas", {}).get(sheet_name, {})
        if not schema:
            session["grid"][sheet_name]           = {"error": "No schema available"}
            session["column_mapping"][sheet_name] = []
            continue

        for col in schema.get("columns", []):
            col["postgres_matched"] = any(
                str(v).upper() in matched_values for v in col.get("sample_non_zero", [])
            )
            embedded_matched = any(
                ext.get("sku", "").upper() in matched_values
                for ext in col.get("embedded_sku_extractions", [])
            )
            col["embedded_postgres_matched"] = embedded_matched
            if embedded_matched:
                col["has_embedded_supplier_sku"] = True

        # Pass 1 — AI identifies date schema pattern
        try:
            text        = await call_ai(build_date_schema_prompt(schema, session["filename"]), label="date_schema")
            clean       = parse_ai_response(text)
            date_schema = json.loads(clean)
        except (json.JSONDecodeError, ValueError) as e:
            date_schema = {"error": f"Parse error: {e}"}
        except Exception as e:
            date_schema = {"error": f"AI error: {e}"}

        if "error" in date_schema:
            session["grid"][sheet_name]           = {"error": date_schema["error"]}
            session["column_mapping"][sheet_name] = []
            continue

        # Python enumerates all sales date columns from the pattern
        sales_cols_1based = find_sales_cols_from_schema(schema, date_schema)
        sales_cols_0based = [c - 1 for c in sales_cols_1based]

        session.setdefault("_date_schemas", {})[sheet_name] = date_schema
        session.setdefault("_sales_cols",   {})[sheet_name] = sales_cols_0based

        # Pass 2 — AI classifies non-date columns
        try:
            text   = await call_ai(build_column_classify_prompt(schema, session["filename"],
                                                                  sales_cols_1based, matched_values), label="column_classify")
            clean  = parse_ai_response(text)
            result = json.loads(clean)
        except (json.JSONDecodeError, ValueError) as e:
            result = {"error": f"Parse error: {e}"}
        except Exception as e:
            result = {"error": f"AI error: {e}"}

        if "error" in result:
            session["grid"][sheet_name]           = {"error": result["error"]}
            session["column_mapping"][sheet_name] = []
            continue

        col_results   = result.get("columns", [])
        col_results   = [{**c, "col": c["col"] - 1} for c in col_results]

        data_start    = date_schema.get("data_start_row", schema["data_start_row"])
        date_axis_row = date_schema.get("date_axis_row", data_start - 1)
        year_boundary = date_schema.get("year_boundary", False)
        year_present  = date_schema.get("year_present", False)

        col_schema_map = {c["col"]: c for c in schema["columns"]}
        sample_vals    = []
        for col_0 in sorted(sales_cols_0based)[:8]:
            col_1 = col_0 + 1
            cs    = col_schema_map.get(col_1, {})
            for h in cs.get("header_stack", []):
                if any(ch.isdigit() for ch in h["value"]):
                    sample_vals.append(h["value"])
                    break

        year_anchors = [
            {"source": "filename", "value": m.group(1)}
            for m in YEAR_RE.finditer(session["filename"])
        ]

        date_axis = {
            "row":                    date_axis_row - 1,
            "col_count":              len(sales_cols_0based),
            "cols":                   sales_cols_0based,
            "sample_values":          sample_vals,
            "format":                 date_schema.get("date_format", "mixed"),
            "year_present":           year_present,
            "interleaved_empty_cols": False,
            "year_boundary_detected": year_boundary,
        }

        rows     = session["_sheets"].get(sheet_name, [])
        embedded = detect_embedded_sku(rows, sales_cols_0based, data_start - 1)

        embedded_by_col = {e["col"]: e for e in embedded}
        for col in schema.get("columns", []):
            col_0 = col["col"] - 1
            if col_0 in embedded_by_col:
                col["embedded_sku_extractions"] = embedded_by_col[col_0]["extractions"]
                col["has_embedded_supplier_sku"] = True

        sku_candidates = []
        for c in col_results:
            col_0 = c["col"]
            col_1 = col_0 + 1
            cs    = col_schema_map.get(col_1, {})
            pre_strings = [
                {"row": h["row"] - 1, "value": h["value"]}
                for h in cs.get("header_stack", [])
            ]
            col_vals = [
                rows[r][col_0]
                for r in range(data_start - 1, min(data_start + 49, len(rows)))
                if col_0 < len(rows[r]) and rows[r][col_0] is not None
            ]
            types   = {}
            for v in col_vals:
                t = classify_cell(v)
                types[t] = types.get(t, 0) + 1
            dominant = max(types, key=types.get) if types else "string"
            sku_candidates.append({
                "col":              col_0,
                "pre_data_strings": pre_strings,
                "dominant_type":    dominant,
                "type_distribution": types,
                "fill_rate":        round(len(col_vals) / 50, 2),
                "sample_values":    [str(v) for v in col_vals[:5]],
            })

        session["grid"][sheet_name] = {
            "date_axis":      date_axis,
            "data_start_row": data_start - 1,
            "sku_candidates": sku_candidates,
            "embedded_sku":   embedded,
            "year_anchors":   year_anchors[:6],
        }

        session["column_mapping"][sheet_name] = [
            {
                "col":                  c["col"],
                "classification":       c["classification"],
                "confidence":           c.get("confidence", "high"),
                "reason":               c.get("reason", ""),
                "has_embedded_supplier_sku": (
                    c.get("has_embedded_supplier_sku", False) or
                    col_schema_map.get(c["col"] + 1, {}).get("has_embedded_supplier_sku", False) or
                    col_schema_map.get(c["col"] + 1, {}).get("embedded_postgres_matched", False)
                ),
            }
            for c in col_results
            if c.get("classification") != "sales_date"
        ]

        # Post-schema: disqualify if no sales date columns found
        if len(sales_cols_0based) == 0:
            session["qualified_sheets"] = [s for s in session["qualified_sheets"] if s != sheet_name]
            session["qualify_results"][sheet_name] = {
                "disqualified": True,
                "reason": "No weekly sales date columns identified after full layout analysis",
                "source": "post_schema",
            }
            session["grid"].pop(sheet_name, None)
            session["column_mapping"].pop(sheet_name, None)

    if not session["qualified_sheets"]:
        file_audit_id = session.get("file_audit_id")
        known_type = detect_known_file_type(
            session.get("filename", ""),
            list(session.get("_sheets", {}).keys())
        )
        audit_status = known_type or "rejected"
        if file_audit_id:
            try:
                sql = build_update_file_audit_full_sql(
                    file_audit_id,
                    {"status": "no_sales_data", "qualify_results": session["qualify_results"]},
                    None, audit_status, None,
                    file_hash=session.get("file_hash"),
                    filename=session.get("filename"),
                )
                await call_postgres(sql)
            except Exception as e:
                session.setdefault("errors", []).append(f"Failed to write {audit_status} status: {e}")
        session["stage"]  = "complete"
        session["status"] = "complete"
        session["result"] = {"status": audit_status, "qualify_results": session["qualify_results"]}
        return

    await stage_identify_retailer(session_id)


async def stage_identify_retailer(session_id: str):
    """Stage 3 — identify retailer by querying retailer_sku_map."""
    session = _sessions[session_id]
    session["stage"]  = "identifying_retailer"
    session["status"] = "running"

    retailer_sku_candidates = set()
    for sheet_name in session["qualified_sheets"]:
        mapping    = session["column_mapping"].get(sheet_name, [])
        cols       = mapping if isinstance(mapping, list) else mapping.get("columns", [])
        grid       = session["grid"].get(sheet_name, {})
        rows       = session["_sheets"].get(sheet_name, [])
        data_start = grid.get("data_start_row", 0)
        for col_info in cols:
            if col_info.get("classification") == "retailer_sku":
                col_idx = col_info.get("col")
                for row in rows[data_start:]:
                    if col_idx < len(row) and row[col_idx] is not None:
                        v = str(row[col_idx]).strip()
                        if v:
                            retailer_sku_candidates.add(v)

    if retailer_sku_candidates:
        try:
            sql  = build_retailer_identify_sql(sorted(retailer_sku_candidates))
            rows = await call_postgres(sql)
            if rows and int(rows[0].get("matches", 0)) >= 3:
                session["retailer"] = rows[0].get("retailer", "").lower()
                session["flags"]["retailer_identification"] = (
                    f"confirmed: {session['retailer']} ({rows[0].get('matches')} matches)"
                )
                await stage_date_config(session_id)
                return
        except Exception as e:
            session["errors"].append(f"Retailer Postgres lookup failed: {e}")

    await stage_identify_retailer_ai(session_id)


async def stage_identify_retailer_ai(session_id: str):
    """Fallback — identify retailer via AI using filename/sheet/header clues."""
    session = _sessions[session_id]
    session["status"] = "running"

    header_strings = []
    for sheet_name in session["qualified_sheets"]:
        grid = session["grid"].get(sheet_name, {})
        for col in grid.get("sku_candidates", []):
            for s in col.get("pre_data_strings", []):
                v = s.get("value", "")
                if v and v not in header_strings:
                    header_strings.append(v)

    try:
        text   = await call_ai(build_retailer_identify_prompt(
            session["filename"], session["qualified_sheets"], header_strings
        ), label="retailer_identify")
        clean  = parse_ai_response(text)
        result = json.loads(clean)
    except (json.JSONDecodeError, ValueError) as e:
        result = {"retailer": None, "confidence": None, "reason": f"Parse error: {e}"}
    except Exception as e:
        result = {"retailer": None, "confidence": None, "reason": f"AI error: {e}"}

    session["retailer"] = result.get("retailer", "").lower() if result.get("retailer") else None
    session["flags"]["retailer_identification"] = (
        f"ai_confirmed: {session['retailer']} ({result.get('confidence', 'unknown')} confidence)"
        if session["retailer"]
        else f"ai_unconfirmed: {result.get('reason', 'unknown')}"
    )
    await stage_date_config(session_id)


async def stage_date_config(session_id: str):
    """Stage 4 — date config. Pure Python, AI only if year ambiguous."""
    session = _sessions[session_id]
    session["stage"]  = "dating"
    session["status"] = "running"

    for sheet_name in session["qualified_sheets"]:
        grid      = session["grid"].get(sheet_name, {})
        date_axis = grid.get("date_axis", {})
        anchors   = grid.get("year_anchors", [])

        if not date_axis:
            session["date_config"][sheet_name] = {"error": "No date axis"}
            continue

        if date_axis.get("year_present"):
            year_value = None
            for a in anchors:
                y = str(a.get("value", ""))[:4]
                if y.isdigit():
                    year_value = int(y)
                    break
            if not year_value:
                for sample in date_axis.get("sample_values", []):
                    parts = str(sample).replace("-", "/").split("/")
                    if len(parts) >= 3:
                        y = parts[-1].strip()[:4]
                        if y.isdigit():
                            yr = int(y)
                            year_value = 2000 + yr if yr < 100 else yr
                            break
            year_boundary = date_axis.get("year_boundary_detected", False)
            session["date_config"][sheet_name] = {
                "date_format":             date_axis["format"],
                "year_present":            True,
                "year_value":              year_value,
                "year_start":              year_value - 1 if year_boundary else year_value,
                "year_inference_strategy": "embedded_in_dates",
                "year_boundary_detected":  year_boundary,
                "normalize_to":            "week_ending_saturday",
                "source":                  "python",
                # Grid fields ingestion needs — stored here so it never reads grid directly
                "date_axis_row":           date_axis.get("row", 0),
                "date_cols":               date_axis.get("cols", []),
                "data_start_row":          grid.get("data_start_row", 1),
            }
            continue

        cross_sheet_anchors = [
            a for other in session["qualified_sheets"] if other != sheet_name
            for a in session["grid"].get(other, {}).get("year_anchors", [])
        ]

        try:
            text   = await call_ai(build_date_prompt(date_axis, anchors, sheet_name,
                                                      filename=session["filename"],
                                                      cross_sheet_anchors=cross_sheet_anchors), label="date_config")
            clean  = parse_ai_response(text)
            result = json.loads(clean)
        except (json.JSONDecodeError, ValueError) as e:
            result = {"error": f"Parse error: {e}"}
        except Exception as e:
            result = {"error": f"AI error: {e}"}

        result["normalize_to"]        = "week_ending_saturday"
        result["source"]              = "ai"
        # Ensure year_boundary_detected and year_start are always present
        # even if AI omits them — fall back to grid values as ground truth
        if "year_boundary_detected" not in result:
            result["year_boundary_detected"] = date_axis.get("year_boundary_detected", False)
        if "year_start" not in result:
            yb = result.get("year_boundary_detected", False)
            yv = result.get("year_value", date.today().year)
            result["year_start"] = yv - 1 if yb else yv
        # Grid fields ingestion needs — stored here so it never reads grid directly
        result["date_axis_row"]  = date_axis.get("row", 0)
        result["date_cols"]      = date_axis.get("cols", [])
        result["data_start_row"] = grid.get("data_start_row", 1)
        session["date_config"][sheet_name] = result

    await stage_multisheet(session_id)


async def stage_multisheet(session_id: str):
    """Stage 5 — multi-sheet flag. Pure Python."""
    session   = _sessions[session_id]
    qualified = session["qualified_sheets"]
    multiple  = len(qualified) > 1
    session["flags"]["multiple_sales_sheets_detected"] = multiple
    if multiple:
        session["flags"]["multiple_sheets_note"] = (
            f"{len(qualified)} qualified sheets detected. "
            "Next stage should compare actual data to determine combine vs dedup."
        )
    await stage_write_audit(session_id)


async def stage_write_audit(session_id: str):
    """Stage 6 — query retailer config, then write to file_audit."""
    session       = _sessions[session_id]
    session["stage"]  = "writing"
    session["status"] = "running"
    file_audit_id = session.get("file_audit_id")
    retailer      = session.get("retailer")

    if not file_audit_id:
        await stage_assemble(session_id)
        return

    file_set_size = 1
    if retailer:
        try:
            sql  = build_query_retailer_config_sql(retailer)
            rows = await call_postgres(sql)
            if rows and rows[0].get("file_set_size") is not None:
                file_set_size = int(rows[0]["file_set_size"] or 1)
            else:
                file_set_size = None  # no active config
        except Exception as e:
            session["errors"].append(f"Failed to query retailer config: {e}")

    await stage_finalize_audit(session_id, file_set_size=file_set_size)


async def stage_finalize_audit(session_id: str, file_set_size: int = 1):
    """Write final status to file_audit."""
    session       = _sessions[session_id]
    file_audit_id = session.get("file_audit_id")
    retailer      = session.get("retailer")

    result = {
        "filename":         session.get("filename"),
        "file_hash":        session.get("file_hash"),
        "retailer":         retailer,
        "file_audit_id":    file_audit_id,
        "qualified_sheets": session.get("qualified_sheets", []),
        "qualify_results":  session.get("qualify_results", {}),
        "grid":             session.get("grid", {}),
        "column_mapping":   session.get("column_mapping", {}),
        "date_config":      session.get("date_config", {}),
        "flags":            session.get("flags", {}),
        "errors":           session.get("errors", []),
    }

    file_set_key = build_file_set_key(retailer, session.get("date_config", {}), session.get("grid", {})) if retailer else None

    # Only trust Postgres-confirmed retailer identification for automated ingestion
    # AI-confirmed retailer requires human review to prevent garbage data
    retailer_id_flag = session.get("flags", {}).get("retailer_identification", "")
    retailer_confirmed = retailer_id_flag.startswith("confirmed:")  # Postgres match only

    if not retailer:                audit_status = "pending_review"
    elif file_set_size is None:     audit_status = "pending_review"
    elif not retailer_confirmed:    audit_status = "pending_review"  # AI-identified — needs human sign-off
    elif file_set_size > 1:         audit_status = "pending_set"
    else:                           audit_status = "discovery_complete"

    sql = build_update_file_audit_full_sql(
        file_audit_id, result, retailer, audit_status, file_set_key,
        file_hash=session.get("file_hash"), filename=session.get("filename"),
    )
    try:
        await call_postgres(sql)
        # Write retailer config if retailer identified
        if retailer and file_audit_id:
            discovery_result = {
                "qualified_sheets": session.get("qualified_sheets", []),
                "column_mapping":   session.get("column_mapping", {}),
                "date_config":      session.get("date_config", {}),
                "flags":            session.get("flags", {}),
            }
            config_sql = build_insert_retailer_config_sql(retailer)
            await call_postgres(config_sql)
    except Exception as e:
        session["errors"].append(f"Failed to write audit/config: {e}")

    await stage_assemble(session_id)


async def stage_assemble(session_id: str):
    """Stage 7 — assemble final result."""
    session = _sessions[session_id]
    session["stage"]  = "complete"
    session["status"] = "complete"
    session["result"] = {
        "filename":         session["filename"],
        "file_hash":        session["file_hash"],
        "retailer":         session.get("retailer"),
        "file_audit_id":    session.get("file_audit_id"),
        "qualified_sheets": session["qualified_sheets"],
        "qualify_results":  session["qualify_results"],
        "grid":             session["grid"],
        "column_mapping":   session["column_mapping"],
        "date_config":      session["date_config"],
        "flags":            session["flags"],
        "errors":           session.get("errors", []),
    }




# ─────────────────────────────────────────────
# ANALYZE ENDPOINT
# ─────────────────────────────────────────────

def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

async def handle_discovery_file_binary(session_id: str, data: bytes, filename: str):
    """Called when file binary arrives from n8n for a discovery session."""
    if session_id not in _sessions:
        return

    session = _sessions[session_id]

    if not filename.lower().endswith((".xlsx", ".xls")):
        session["stage"]  = "complete"
        session["status"] = "complete"
        session["result"] = {"error": f"'{filename}' is not an Excel file"}
        return

    # Immediate filename check — internal D365 inventory exports never need
    # sales-data qualification, so route them before opening the workbook at all.
    known_type = detect_known_file_type(filename, [])
    if known_type:
        file_audit_id = session.get("file_audit_id")
        if not session.get("file_hash"):
            session["file_hash"] = file_hash(data)
        if file_audit_id:
            try:
                sql = build_update_file_audit_full_sql(
                    file_audit_id,
                    {"status": "routed_by_filename"},
                    None, known_type, None,
                    file_hash=session.get("file_hash"),
                    filename=filename,
                )
                await call_postgres(sql)
            except Exception as e:
                session.setdefault("errors", []).append(f"Failed to write {known_type} status: {e}")
        session["stage"]  = "complete"
        session["status"] = "complete"
        session["result"] = {"status": known_type}
        return

    MAX_UPLOAD_BYTES = 50 * 1024 * 1024
    if len(data) > MAX_UPLOAD_BYTES:
        session["stage"]  = "complete"
        session["status"] = "complete"
        session["result"] = {"error": "File too large — max 50MB"}
        return

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        session["stage"]  = "complete"
        session["status"] = "complete"
        session["result"] = {"error": f"Cannot open {filename}: {e}"}
        return

    sheets = {}
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        sheets[sheet_name] = list(ws.iter_rows(values_only=True))

    session["_sheets"]   = sheets
    session["_raw_data"] = data
    if not session.get("file_hash"):
        session["file_hash"] = file_hash(data)

    # Dedup check — skip if this hash belongs to a different file_audit row
    try:
        dedup_rows = await call_postgres(build_dedup_check_sql(session["file_hash"]))
        if dedup_rows and str(dedup_rows[0].get("id")) != str(session.get("file_audit_id")):
            existing_id     = dedup_rows[0].get("id")
            existing_status = dedup_rows[0].get("status")
            session["stage"]  = "complete"
            session["status"] = "complete"
            session["result"] = {
                "skipped":   True,
                "reason":    f"File already processed — existing row {existing_id} has status '{existing_status}'",
                "file_hash": session["file_hash"],
            }
            return
    except Exception as e:
        session["errors"].append(f"Dedup check failed: {e}")

    await stage_qualify(session_id)


@router.post("/analyze")
async def analyze_from_audit(request_body: dict, background_tasks: BackgroundTasks = BackgroundTasks()):
    """
    Trigger discovery from a file_audit_id — file is fetched from MinIO via n8n.
    Used by the polling workflow for received files.
    """
    file_audit_id = request_body.get("file_audit_id")
    if not file_audit_id:
        raise HTTPException(status_code=400, detail="file_audit_id required")

    try:
        safe_id = _validate_uuid(file_audit_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file_audit_id format")

    # Fetch audit row to get filename and check dedup
    try:
        rows = await call_postgres(build_fetch_audit_row_sql(safe_id))
        if not rows:
            raise HTTPException(status_code=404, detail=f"file_audit row {safe_id} not found")
        audit_row = rows[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch audit row: {e}")

    filename  = audit_row.get("filename", "unknown.xlsx")
    file_hash_val = audit_row.get("file_hash")

    # Atomically claim this row — flips received -> analyzing.
    # If another concurrent poll already claimed it, bail out cleanly
    # instead of running discovery twice on the same file.
    try:
        claim_rows = await call_postgres(build_claim_file_audit_sql(safe_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to claim audit row: {e}")

    if not claim_rows:
        return JSONResponse(content={
            "status": "skipped",
            "reason": f"file_audit row {safe_id} is no longer 'received' — already claimed or processed",
        })

    session_id = str(uuid.uuid4())
    _sessions[session_id] = {
        "stage":            "accepted",
        "status":           "running",
        "filename":         filename,
        "file_hash":        file_hash_val,
        "file_audit_id":    safe_id,
        "retailer":         None,
        "qualified_sheets": [],
        "qualify_results":  {},
        "grid":             {},
        "postgres_results": {},
        "column_mapping":   {},
        "date_config":      {},
        "flags":            {},
        "errors":           [],
        "result":           None,
        "_sheets":          None,   # populated when file arrives
        "_raw_data":        None,
        "_pending_jobs":    set(),
        "created_at":       time.time(),
    }

    # Fire fetch-file webhook — file binary will arrive at /file/{job_id}
    # But discovery needs the binary before it can start — use a flag to wait
    file_job_id = str(uuid.uuid4())
    _jobs[file_job_id] = {
        "session_id": session_id,
        "stage":      "fetch_file_binary",
        "audit_id":   safe_id,
        "pipeline":   "discovery",
        "created_at": time.time(),
    }
    err = await fire_fetch_file_webhook(file_job_id, safe_id)
    if err:
        _sessions.pop(session_id, None)
        _jobs.pop(file_job_id, None)
        raise HTTPException(status_code=500, detail=f"Failed to fetch file: {err}")

    _sessions[session_id]["_pending_jobs"].add(file_job_id)

    async def _wait_for_file():
        """
        Poll for completion. The actual file binary is delivered to
        POST /file/{job_id} (in ingestion.py), which invokes
        handle_discovery_file_binary directly as a background task.
        This loop just waits for that task to finish — it never overwrites
        a session that has progressed past the initial 'accepted' stage,
        since that would mean real processing is underway (e.g. AI column
        classification across multiple sheets, which can legitimately take
        a while) and must not be stomped by an artificial timeout.
        """
        for _ in range(300):  # wait up to 5 minutes
            await asyncio.sleep(1)
            session = _sessions.get(session_id)
            if not session:
                return
            if session.get("status") in ("complete", "failed"):
                return
        # Timed out — only treat as a real failure if the file binary itself
        # never arrived (session never progressed past "accepted"/"running"
        # with no sheets loaded). Otherwise leave the session alone; it is
        # still legitimately processing and will complete on its own.
        session = _sessions.get(session_id)
        if session and session.get("_sheets") is None:
            session["stage"]  = "complete"
            session["status"] = "failed"
            session["result"] = {"error": "Timed out waiting for file binary from MinIO"}

    background_tasks.add_task(_wait_for_file)

    return JSONResponse(content={
        "status":     "accepted",
        "session_id": session_id,
        "poll_url":   f"/analyze/status/{session_id}",
    })



@router.get("/analyze/status/{session_id}")
async def analyze_status(session_id: str):
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    session = _sessions[session_id]
    return JSONResponse(content={
        "session_id": session_id,
        "stage":      session.get("stage", "unknown"),
        "status":     session.get("status", "unknown"),
        "filename":   session["filename"],
        "result":     session["result"],
    })


@router.get("/health")
def health():
    return {"status": "ok", "version": "3.2.0"}
