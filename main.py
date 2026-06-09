from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from typing import List
import openpyxl
import hashlib
import io
import re
import uuid
import asyncio
import urllib.request
import json
import os
from datetime import datetime

app = FastAPI(title="Sheet Discovery Service", version="2.0.0")

# ─────────────────────────────────────────────
# JOB STORE — for Python/n8n webhook collaboration
# ─────────────────────────────────────────────
_qualify_jobs: dict = {}

N8N_QUALIFY_WEBHOOK = os.environ.get(
    "N8N_QUALIFY_WEBHOOK",
    "http://n8n:5678/webhook/qualify-sheet"
)

# ─────────────────────────────────────────────
# CELL CLASSIFICATION — pure structural
# ─────────────────────────────────────────────

DATE_RANGE_RE  = re.compile(r"^\d{1,2}/\d{1,2}[-–]\d{1,2}/\d{1,2}$")
FISCAL_WEEK_RE = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+wk\s*\d+$", re.IGNORECASE)
WK_NUMBER_RE   = re.compile(r"^wk\s*\d+$", re.IGNORECASE)
YEAR_RE        = re.compile(r"\b(20\d{2})\b")

def classify_cell(val) -> str:
    if val is None:                return "empty"
    if isinstance(val, bool):      return "bool"
    if isinstance(val, datetime):  return "datetime"
    if isinstance(val, int):       return "integer"
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
        "datetime", "date_range_string",
        "fiscal_week_label", "week_number_label"
    )


# ─────────────────────────────────────────────
# GRID STRUCTURE DETECTION
# ─────────────────────────────────────────────

def find_date_axis(rows) -> dict | None:
    best = {"row": None, "count": 0, "cols": [], "samples": [], "format": None}

    for row_idx, row in enumerate(rows[:15]):
        date_cols = [(ci, v) for ci, v in enumerate(row) if is_date_like(v)]
        if len(date_cols) > best["count"]:
            formats = set(classify_cell(v) for _, v in date_cols)
            best = {
                "row":     row_idx,
                "count":   len(date_cols),
                "cols":    [ci for ci, _ in date_cols],
                "samples": [str(v) for _, v in date_cols[:8]],
                "format":  list(formats)[0] if len(formats) == 1 else "mixed",
            }

    if best["count"] < 2:
        return None

    cols = best["cols"]
    year_present = (
        best["format"] == "datetime" or
        any(YEAR_RE.search(s) for s in best["samples"])
    )

    # Detect interleaved empty columns
    interleaved = False
    if len(cols) >= 3:
        gaps = [cols[i+1] - cols[i] for i in range(len(cols)-1)]
        interleaved = len(set(gaps)) == 1 and gaps[0] == 2

    # Detect year boundary — date range strings spanning December and January
    # e.g. "12/21-12/27" and "1/4-1/10" in same sheet means two calendar years
    year_boundary_detected = False
    if best["format"] in ("date_range_string", "mixed"):
        months = set()
        for s in best["samples"]:
            # Extract first month from date range string like "12/21-12/27" or "1/4-1/10"
            m = re.match(r'^(\d{1,2})/', s.strip())
            if m:
                months.add(int(m.group(1)))
        # If both December (12) and January (1) appear, year boundary exists
        if 12 in months and 1 in months:
            year_boundary_detected = True

    return {
        "row":                    best["row"],
        "col_count":              best["count"],
        "cols":                   cols,
        "sample_values":          best["samples"],
        "format":                 best["format"],
        "year_present":           year_present,
        "interleaved_empty_cols": interleaved,
        "year_boundary_detected": year_boundary_detected,
    }


def find_data_start_row(rows, date_axis_row: int, date_cols: list) -> int:
    """First row after the header block that has numeric values at date cols."""
    for row_idx in range(date_axis_row + 1, min(date_axis_row + 10, len(rows))):
        row = rows[row_idx]
        vals = [row[dc] for dc in date_cols if dc < len(row) and row[dc] is not None]
        numeric = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if numeric:
            return row_idx
    return date_axis_row + 1


def find_sku_candidates(rows, date_axis_row: int, date_cols: list) -> list:
    """
    Report all columns left of the date axis as candidates.
    No scoring, no decisions — Python describes, AI decides.
    Each candidate includes: col index, label, dominant type,
    fill rate, and sample values.
    """
    if not date_cols:
        return []

    left_boundary = min(date_cols)
    data_start = find_data_start_row(rows, date_axis_row, date_cols)
    data_rows = [rows[r] for r in range(data_start, min(data_start + 50, len(rows)))]
    total_data_rows = len(data_rows)

    candidates = []

    for col_idx in range(left_boundary):
        col_vals = [
            row[col_idx]
            for row in data_rows
            if col_idx < len(row) and row[col_idx] is not None
        ]

        if not col_vals:
            continue

        # Type distribution
        types = [classify_cell(v) for v in col_vals]
        type_counts = {}
        for t in types:
            type_counts[t] = type_counts.get(t, 0) + 1
        dominant = max(type_counts, key=type_counts.get)

        # Fill rate
        fill_rate = round(len(col_vals) / total_data_rows, 2) if total_data_rows > 0 else 0

        # All strings above data start — AI decides which is the column label
        pre_data_strings = [
            {"row": row_idx, "value": rows[row_idx][col_idx]}
            for row_idx in range(data_start)
            if col_idx < len(rows[row_idx])
            and isinstance(rows[row_idx][col_idx], str)
            and rows[row_idx][col_idx].strip()
        ]

        # Sample values — first 5 non-null from data rows
        sample = [str(v) for v in col_vals[:5]]

        candidates.append({
            "col":              col_idx,
            "pre_data_strings": pre_data_strings,
            "dominant_type":    dominant,
            "type_distribution": type_counts,
            "fill_rate":        fill_rate,
            "sample_values":    sample,
        })

    return candidates



# ─────────────────────────────────────────────
# CROSSHAIR SAMPLING — raw, no verdicts
# ─────────────────────────────────────────────

def sample_crosshair(rows, date_cols: list, sku_col: int, data_start_row: int) -> dict:
    """
    Sample values at true crosshair intersections.
    Spread sample across whole sheet, not just top rows.
    Returns raw values and row-level type distribution — no verdict.
    """
    total_rows = len(rows)

    # Build a spread of row indices across the whole sheet
    sample_indices = []
    step = max(1, (total_rows - data_start_row) // 20)
    for i in range(data_start_row, total_rows, step):
        sample_indices.append(i)
        if len(sample_indices) >= 40:
            break

    raw_values = []
    row_type_counts = {
        "integer": 0,
        "float_0_to_1": 0,
        "float_above_1": 0,
        "empty": 0,
        "string": 0,
    }

    for row_idx in sample_indices:
        row = rows[row_idx]
        if sku_col >= len(row) or row[sku_col] is None:
            continue

        row_vals = []
        for dc in date_cols[:8]:
            if dc >= len(row):
                continue
            val = row[dc]
            ct = classify_cell(val)
            if ct in row_type_counts:
                row_type_counts[ct] += 1
            if val is not None:
                row_vals.append(val)

        numeric = [v for v in row_vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if numeric:
            raw_values.extend(numeric)

    return {
        "sample_values":        raw_values[:20],
        "row_type_distribution": row_type_counts,
    }


# ─────────────────────────────────────────────
# VOCABULARY COLLECTION — all strings, no filtering
# ─────────────────────────────────────────────

def collect_all_vocabulary(rows, date_cols: list, sku_col: int | None, data_start_row: int) -> dict:
    """
    Collect all string values from the sheet.
    No filtering, no decisions — everything goes to the AI.
    Organised by where in the sheet it came from.
    """
    header_strings = set()
    column_label_strings = set()
    inline_strings = set()

    left_boundary = min(date_cols) if date_cols else 999

    for row_idx, row in enumerate(rows):
        for col_idx, val in enumerate(row):
            if not isinstance(val, str) or not val.strip():
                continue
            s = val.strip()

            if row_idx < data_start_row:
                # Header area
                if col_idx < left_boundary:
                    column_label_strings.add(s)
                else:
                    header_strings.add(s)
            else:
                # Data area — inline strings (section headers, totals, footnotes, metrics)
                if col_idx < left_boundary:
                    inline_strings.add(s)

    def sample_strings(s: set, n: int = 10) -> list:
        items = sorted(s)
        if len(items) <= n:
            return items
        step = len(items) / n
        return [items[int(i * step)] for i in range(n)]

    return {
        "header_strings":       sorted(header_strings),
        "column_label_strings": sorted(column_label_strings),
        "inline_strings":       sample_strings(inline_strings, 10),
    }



# ─────────────────────────────────────────────
# EMBEDDED SKU DETECTION
# ─────────────────────────────────────────────

EMBEDDED_PATTERNS = [
    # 285768 - LEVEL UP HEADPHONE
    ("integer_dash_description",
     re.compile(r'^(\d{4,10})\s*[-\u2013]\s*(.{3,})$')),

    # MZX1010-BLK ALTEC LANS CLIP OPEN — alphanumeric SKU with dash, then description
    ("alphanumeric_space_description",
     re.compile(r'^([A-Z0-9]{2,}-[A-Z0-9\-]{2,})\s+(.{3,})$', re.IGNORECASE)),

    # LEVEL UP HEADPHONE   LU731-WG — description then SKU with dash (double space separator)
    ("description_space_alphanumeric",
     re.compile(r'^(.{3,})\s{2,}([A-Z0-9]{2,}-[A-Z0-9\-]{2,})\s*$', re.IGNORECASE)),

    # Level Up Headphones LUP052 — description then no-dash alphanumeric SKU (single space, end of string)
    ("description_space_nodash_sku",
     re.compile(r'^(.{5,})\s+([A-Z]{2,}\d{3,})\s*$', re.IGNORECASE)),
]


def detect_embedded_sku(rows, date_cols: list, data_start_row: int) -> list:
    """
    Check columns left of the date axis for cells where SKU and description
    are fused into one string. Returns all candidate columns with match counts.
    No thresholds — AI decides if embedded SKU exists.
    """
    if not date_cols:
        return []

    left_boundary = min(date_cols)
    embedded_candidates = []

    for col_idx in range(left_boundary):
        col_vals = [
            str(rows[r][col_idx]).strip()
            for r in range(data_start_row, min(data_start_row + 30, len(rows)))
            if col_idx < len(rows[r])
            and rows[r][col_idx] is not None
            and isinstance(rows[r][col_idx], str)
        ]

        if len(col_vals) < 3:
            continue

        # Try all patterns, collect all matches — no threshold, AI decides
        col_matches = []
        seen = set()
        for pattern_name, pattern_re in EMBEDDED_PATTERNS:
            for val in col_vals:
                if val not in seen:
                    m = pattern_re.match(val)
                    if m:
                        seen.add(val)
                        col_matches.append({
                            "raw":         val,
                            "sku":         m.group(1).strip(),
                            "description": m.group(2).strip(),
                            "pattern":     pattern_name,
                        })

        if col_matches:
            embedded_candidates.append({
                "col":               col_idx,
                "total_values":      len(col_vals),
                "matched_values":    len(col_matches),
                "sample_extractions": col_matches[:4],
            })

    return embedded_candidates


# ─────────────────────────────────────────────
# YEAR ANCHORS
# ─────────────────────────────────────────────

def find_year_anchors(filename: str, rows: list) -> list:
    anchors = []
    for m in YEAR_RE.finditer(filename):
        anchors.append({"source": "filename", "value": m.group(1)})
    for row_idx, row in enumerate(rows[:5]):
        for col_idx, val in enumerate(row):
            if isinstance(val, datetime):
                anchors.append({"source": "cell", "row": row_idx, "col": col_idx,
                                 "value": str(val.date())})
            elif isinstance(val, str):
                for m in YEAR_RE.finditer(val):
                    anchors.append({"source": "cell", "row": row_idx, "col": col_idx,
                                     "value": m.group(1)})
    return anchors[:6]


# ─────────────────────────────────────────────
# DATA ROW COUNT
# ─────────────────────────────────────────────

def count_data_rows(rows, date_cols: list, sku_col: int, data_start_row: int) -> dict:
    count = 0
    for row_idx in range(data_start_row, len(rows)):
        row = rows[row_idx]
        if sku_col >= len(row) or row[sku_col] is None:
            continue
        date_vals = [row[dc] for dc in date_cols if dc < len(row) and row[dc] is not None]
        if date_vals:
            count += 1
    return {"count": count, "start_row": data_start_row}


# ─────────────────────────────────────────────
# PER SHEET ANALYSIS
# ─────────────────────────────────────────────

def analyze_sheet(ws, sheet_name: str, filename: str) -> dict:
    rows = list(ws.iter_rows(values_only=True))

    date_axis = find_date_axis(rows)

    if not date_axis:
        # No date axis found — still collect vocabulary for AI
        all_strings = sorted(set(
            str(v).strip() for row in rows[:30]
            for v in row if isinstance(v, str) and v.strip()
        ))
        return {
            "sheet_name":     sheet_name,
            "filename":       filename,
            "date_axis":      None,
            "sku_candidates": [],
            "embedded_sku":   None,
            "crosshair":      None,
            "vocabulary":     {"header_strings": all_strings,
                               "column_label_strings": [],
                               "inline_strings": []},
            "year_anchors":   find_year_anchors(filename, rows),
            "data_rows":      {"count": 0, "start_row": None},
            "data_start_row": None,
            "row_count":      len(rows),
            "col_count":      len(rows[0]) if rows else 0,
        }

    date_cols      = date_axis["cols"]
    data_start     = find_data_start_row(rows, date_axis["row"], date_cols)
    sku_candidates = find_sku_candidates(rows, date_axis["row"], date_cols)
    embedded_sku   = detect_embedded_sku(rows, date_cols, data_start)

    # Use first integer-dominant candidate as anchor for crosshair sampling
    # This is only for internal sampling — AI decides the real SKU col
    anchor_col = next(
        (c["col"] for c in sku_candidates if c["dominant_type"] == "integer"),
        sku_candidates[0]["col"] if sku_candidates else 0
    )

    crosshair    = sample_crosshair(rows, date_cols, anchor_col, data_start)
    vocabulary   = collect_all_vocabulary(rows, date_cols, anchor_col, data_start)
    year_anchors = find_year_anchors(filename, rows)
    data_rows    = count_data_rows(rows, date_cols, anchor_col, data_start)

    return {
        "sheet_name":     sheet_name,
        "filename":       filename,
        "row_count":      len(rows),
        "col_count":      len(rows[0]) if rows else 0,
        "date_axis":      date_axis,
        "sku_candidates": sku_candidates,
        "embedded_sku":   embedded_sku,
        "data_start_row": data_start,
        "crosshair":      crosshair,
        "vocabulary":     vocabulary,
        "year_anchors":   year_anchors,
        "data_rows":      data_rows,
    }


# ─────────────────────────────────────────────
# CROSS SHEET ANALYSIS
# ─────────────────────────────────────────────

def cross_sheet_analysis(sheets: list) -> list | None:
    # Only compare sheets that have a date axis and crosshair data
    viable = [s for s in sheets if s["date_axis"] and s["crosshair"]]
    if len(viable) < 2:
        return None

    results = []
    for i in range(len(viable)):
        for j in range(i + 1, len(viable)):
            a, b = viable[i], viable[j]
            a_dates = set(a["date_axis"]["sample_values"])
            b_dates = set(b["date_axis"]["sample_values"])
            date_overlap = bool(a_dates & b_dates)

            results.append({
                "sheets":       [a["sheet_name"], b["sheet_name"]],
                "date_overlap": date_overlap,
                "a_row_types":  a["crosshair"]["row_type_distribution"],
                "b_row_types":  b["crosshair"]["row_type_distribution"],
            })

    return results if results else None


# ─────────────────────────────────────────────
# ENDPOINT
# ─────────────────────────────────────────────

def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

@app.post("/discover")
async def discover(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="At least one file required")

    results = []

    for upload in files:
        if not upload.filename.lower().endswith((".xlsx", ".xls")):
            raise HTTPException(status_code=400,
                                detail=f"'{upload.filename}' is not an Excel file")

        data = await upload.read()

        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:
            raise HTTPException(status_code=400,
                                detail=f"Cannot open {upload.filename}: {e}")

        sheets = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheets.append(analyze_sheet(ws, sheet_name, upload.filename))

        results.append({
            "filename":             upload.filename,
            "file_hash":            file_hash(data),
            "sheet_count":          len(sheets),
            "sheets":               sheets,
            "cross_sheet_analysis": cross_sheet_analysis(sheets),
        })

    return JSONResponse(content={"status": "ok", "files": results})



# ─────────────────────────────────────────────
# SKU EXTRACTION ENDPOINT
# ─────────────────────────────────────────────

def extract_all_skus_from_sheet(ws, sheet_name: str) -> dict:
    """
    Extract all unique SKU prospect values from every candidate column.
    Returns full dataset — not a sample. Used for retailer SKU reconciliation.
    """
    rows = list(ws.iter_rows(values_only=True))

    date_axis = find_date_axis(rows)
    if not date_axis:
        return {"sheet_name": sheet_name, "columns": []}

    date_cols  = date_axis["cols"]
    data_start = find_data_start_row(rows, date_axis["row"], date_cols)
    left_boundary = min(date_cols)

    columns = []

    for col_idx in range(left_boundary):
        # All non-null values in this column from data_start onward
        all_values = []
        for row in rows[data_start:]:
            if col_idx < len(row) and row[col_idx] is not None:
                all_values.append(row[col_idx])

        if not all_values:
            continue

        # Pre-data strings for label identification
        pre_data_strings = [
            {"row": row_idx, "value": rows[row_idx][col_idx]}
            for row_idx in range(data_start)
            if col_idx < len(rows[row_idx])
            and isinstance(rows[row_idx][col_idx], str)
            and rows[row_idx][col_idx].strip()
        ]

        # Unique values
        seen = set()
        unique_values = []
        for v in all_values:
            key = str(v).strip()
            if key not in seen:
                seen.add(key)
                unique_values.append(v)

        # Run embedded SKU extraction on all string values
        embedded_extractions = []
        seen_raw = set()
        for val in all_values:
            if not isinstance(val, str):
                continue
            s = val.strip()
            if s in seen_raw:
                continue
            seen_raw.add(s)
            for pattern_name, pattern_re in EMBEDDED_PATTERNS:
                m = pattern_re.match(s)
                if m:
                    # description_space_* patterns have description in group(1), SKU in group(2)
                    # all other patterns have SKU in group(1), description in group(2)
                    if pattern_name.startswith("description_space"):
                        sku, desc = m.group(2).strip(), m.group(1).strip()
                    else:
                        sku, desc = m.group(1).strip(), m.group(2).strip()
                    embedded_extractions.append({
                        "raw":         s,
                        "sku":         sku,
                        "description": desc,
                        "pattern":     pattern_name,
                    })
                    break  # first matching pattern wins

        col_entry = {
            "col":               col_idx,
            "pre_data_strings":  pre_data_strings,
            "unique_values":     [str(v) for v in unique_values],
            "total_rows":        len(all_values),
        }

        if embedded_extractions:
            col_entry["embedded_extractions"] = embedded_extractions

        columns.append(col_entry)

    return {"sheet_name": sheet_name, "columns": columns}


@app.post("/extract_skus")
async def extract_skus(files: List[UploadFile] = File(...)):
    """
    Extract all unique SKU prospect values from every candidate column.
    Full dataset — not sampled. Used for building retailer_sku_map via Postgres reconciliation.
    """
    if not files:
        raise HTTPException(status_code=400, detail="At least one file required")

    results = []

    for upload in files:
        if not upload.filename.lower().endswith((".xlsx", ".xls")):
            raise HTTPException(status_code=400,
                                detail=f"'{upload.filename}' is not an Excel file")

        data = await upload.read()

        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:
            raise HTTPException(status_code=400,
                                detail=f"Cannot open {upload.filename}: {e}")

        sheets = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheets.append(extract_all_skus_from_sheet(ws, sheet_name))

        results.append({
            "filename": upload.filename,
            "file_hash": file_hash(data),
            "sheets": sheets,
        })

    return JSONResponse(content={"status": "ok", "files": results})


# ─────────────────────────────────────────────
# QUALIFY ENDPOINT — AI disqualification test
# ─────────────────────────────────────────────

def extract_qualify_signals_from_rows(rows: list, sheet_name: str, filename: str) -> dict:
    """
    Extract disqualification signals from pre-loaded rows.
    Core logic used by both sequential and parallel qualify flows.
    """
    crosshair_sample = []
    dominant_type = None
    for row in rows[:20]:
        nums = [v for v in row if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(nums) >= 3:
            crosshair_sample = nums[:8]
            has_float = any(isinstance(v, float) and v > 1 for v in nums)
            has_pct   = any(isinstance(v, float) and 0 < v <= 1 for v in nums)
            has_int   = any(isinstance(v, int) for v in nums)
            if has_float:   dominant_type = "float_above_1"
            elif has_pct:   dominant_type = "float_0_to_1"
            elif has_int:   dominant_type = "integer"
            break

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
    }


def extract_disqualification_signals(ws, sheet_name: str, filename: str) -> dict:
    """Wrapper — loads rows from worksheet then delegates to core function."""
    rows = list(ws.iter_rows(values_only=True))
    return extract_qualify_signals_from_rows(rows, sheet_name, filename)

def _UNUSED_old_extract(ws, sheet_name, filename):
    rows = list(ws.iter_rows(values_only=True))

    # Crosshair — sample values from top-left data area
    # Find first row with numeric values and sample across columns
    crosshair_sample = []
    dominant_type = None
    for row in rows[:20]:
        nums = [v for v in row if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(nums) >= 3:
            crosshair_sample = nums[:8]
            has_float = any(isinstance(v, float) and v > 1 for v in nums)
            has_pct   = any(isinstance(v, float) and 0 < v <= 1 for v in nums)
            has_int   = any(isinstance(v, int) for v in nums)
            if has_float:   dominant_type = "float_above_1"
            elif has_pct:   dominant_type = "float_0_to_1"
            elif has_int:   dominant_type = "integer"
            break

    # Column labels — all strings in first 6 rows
    column_labels = set()
    header_strings = set()
    for row in rows[:6]:
        for val in row:
            if isinstance(val, str) and val.strip():
                s = val.strip()
                if len(s) < 60:
                    column_labels.add(s)

    # Inline strings — strings in data rows (rows 6-20)
    inline_strings = set()
    for row in rows[6:20]:
        for val in row:
            if isinstance(val, str) and val.strip() and len(val.strip()) < 80:
                inline_strings.add(val.strip())

    # Sort by length ascending — short keywords like CFP, $$$, Forecast bubble up
    # Long narrative strings add noise, not signal
    return {
        "sheet_name":      sheet_name,
        "filename":        filename,
        "crosshair_sample": crosshair_sample,
        "dominant_type":   dominant_type,
        "column_labels":   sorted(column_labels, key=len)[:10],
        "inline_strings":  sorted(inline_strings, key=len)[:10],
    }


def build_qualify_prompt(signals: dict) -> str:
    sheet_name    = signals["sheet_name"]
    filename      = signals["filename"]
    dominant_type = signals["dominant_type"]
    crosshair     = signals["crosshair_sample"]
    col_labels    = signals["column_labels"]
    inline        = signals["inline_strings"]

    return f"""You are the gatekeeper of a retail sales data ingestion pipeline. A sheet has arrived and you must determine if it should be disqualified.

A sheet should be disqualified if it does NOT contain unit sales data. Unit sales data has integer values at the intersection of product identifiers and date columns.

Disqualify if any of the following are true:
- Crosshair values are decimals above 1 — dollar revenue
- Crosshair values are between 0 and 1 — percentage metrics
- Column labels contain $$$ or $$ — dollar sheet
- Sheet name contains CFP, Forecast, FCST, Projection, Order
- Vocabulary contains forecast or projection language

EVIDENCE:
Sheet name: {sheet_name}
Filename: {filename}
Crosshair sample values: {crosshair}
Dominant crosshair type: {dominant_type}
Column labels: {col_labels}
Inline strings: {inline}

Respond with JSON only:
{{
  "disqualified": true or false,
  "reason": "one sentence explanation"
}}"""


@app.post("/qualify")
async def qualify(files: List[UploadFile] = File(...)):
    """
    For each sheet in the uploaded file, extract disqualification signals
    and fire to n8n webhook. Returns a session_id immediately.
    Caller polls /qualify/status/{session_id} for results.
    """
    if not files:
        raise HTTPException(status_code=400, detail="At least one file required")

    session_id = str(uuid.uuid4())
    session = {
        "status":   "pending",
        "filename": None,
        "total":    0,
        "done":     0,
        "results":  [],
    }
    _qualify_jobs[session_id] = session

    for upload in files:
        if not upload.filename.lower().endswith((".xlsx", ".xls")):
            _qualify_jobs.pop(session_id, None)
            raise HTTPException(status_code=400,
                                detail=f"'{upload.filename}' is not an Excel file")

        data = await upload.read()

        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:
            _qualify_jobs.pop(session_id, None)
            raise HTTPException(status_code=400,
                                detail=f"Cannot open {upload.filename}: {e}")

        session["filename"] = upload.filename
        session["total"]    = len(wb.sheetnames)

        for sheet_name in wb.sheetnames:
            ws   = wb[sheet_name]
            rows = list(ws.iter_rows(values_only=True))
            signals = extract_qualify_signals_from_rows(rows, sheet_name, upload.filename)

            job_id = str(uuid.uuid4())
            # Store job keyed by job_id, linked to session
            _qualify_jobs[job_id] = {
                "session_id": session_id,
                "sheet_name": sheet_name,
                "signals":    signals,
                "verdict":    None,
                "error":      None,
            }

            payload = json.dumps({"job_id": job_id, "signals": signals}).encode()
            try:
                req = urllib.request.Request(
                    N8N_QUALIFY_WEBHOOK,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                _qualify_jobs[job_id]["error"] = f"Failed to reach n8n: {e}"
                session["done"]   += 1
                session["results"].append(_qualify_jobs.pop(job_id))

    # Check if all jobs were already error-failed synchronously
    if session["done"] >= session["total"]:
        session["status"] = "complete"

    return JSONResponse(content={
        "status":     "accepted",
        "session_id": session_id,
        "total_sheets": session["total"],
        "poll_url":   f"/qualify/status/{session_id}",
    })


@app.get("/qualify/status/{session_id}")
async def qualify_status(session_id: str):
    """
    Poll for qualify results. Returns partial results as they arrive.
    status: pending | complete
    """
    if session_id not in _qualify_jobs:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    session = _qualify_jobs[session_id]

    return JSONResponse(content={
        "session_id": session_id,
        "status":     session["status"],
        "filename":   session["filename"],
        "total":      session["total"],
        "done":       session["done"],
        "results":    session["results"],
    })


@app.post("/response/{job_id}")
async def qualify_response(job_id: str, request_body: dict):
    """
    n8n posts the AI verdict back here keyed by job_id.
    Updates the parent session with the result.
    """
    if job_id not in _qualify_jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found or already completed")

    job        = _qualify_jobs.pop(job_id)
    session_id = job["session_id"]
    job["verdict"] = request_body

    if session_id in _qualify_jobs:
        session = _qualify_jobs[session_id]
        session["results"].append(job)
        session["done"] += 1
        if session["done"] >= session["total"]:
            session["status"] = "complete"

    return JSONResponse(content={"status": "ok", "job_id": job_id})

@app.get("/health")
def health():
    return {"status": "ok"}
