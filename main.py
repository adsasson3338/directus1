from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from typing import List
import openpyxl
import hashlib
import io
import re
from datetime import datetime

app = FastAPI(title="Sheet Discovery Service", version="2.0.0")

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

    return {
        "row":                  best["row"],
        "col_count":            best["count"],
        "cols":                 cols,
        "sample_values":        best["samples"],
        "format":               best["format"],
        "year_present":         year_present,
        "interleaved_empty_cols": interleaved,
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

        # Label — scan rows from data_start upward
        label = None
        for row_idx in range(data_start - 1, -1, -1):
            if col_idx < len(rows[row_idx]):
                v = rows[row_idx][col_idx]
                if isinstance(v, str) and v.strip():
                    label = v.strip()
                    break

        # Sample values — first 5 non-null from data rows
        sample = [str(v) for v in col_vals[:5]]

        candidates.append({
            "col":          col_idx,
            "label":        label,
            "dominant_type": dominant,
            "type_distribution": type_counts,
            "fill_rate":    fill_rate,
            "sample_values": sample,
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

    return {
        "header_strings":       sorted(header_strings),
        "column_label_strings": sorted(column_label_strings),
        "inline_strings":       sorted(inline_strings),
    }



# ─────────────────────────────────────────────
# EMBEDDED SKU DETECTION
# ─────────────────────────────────────────────

EMBEDDED_PATTERNS = [
    # 285768 - LEVEL UP HEADPHONE
    ("integer_dash_description",
     re.compile(r'^(\d{4,10})\s*[-\u2013]\s*(.{3,})$')),

    # LU731-WG LEVEL UP HEADPHONE
    ("alphanumeric_space_description",
     re.compile(r'^([A-Z0-9]{2,}-[A-Z0-9\-]{2,})\s+(.{3,})$', re.IGNORECASE)),

    # LEVEL UP HEADPHONE   LU731-WG
    ("description_space_alphanumeric",
     re.compile(r'^(.{3,})\s{2,}([A-Z0-9]{2,}-[A-Z0-9\-]{2,})\s*$', re.IGNORECASE)),
]


def detect_embedded_sku(rows, date_cols: list, data_start_row: int) -> dict | None:
    """
    Check columns left of the date axis for cells where SKU and description
    are fused into one string. Pure structural — no vocabulary assumptions.
    """
    if not date_cols:
        return None

    left_boundary = min(date_cols)

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

        for pattern_name, pattern_re in EMBEDDED_PATTERNS:
            matches = []
            for val in col_vals:
                m = pattern_re.match(val)
                if m:
                    matches.append({
                        "raw":         val,
                        "sku":         m.group(1).strip(),
                        "description": m.group(2).strip(),
                    })

            match_ratio = len(matches) / len(col_vals)
            if match_ratio >= 0.5 and len(matches) >= 3:
                return {
                    "col":                col_idx,
                    "pattern":            pattern_name,
                    "match_ratio":        round(match_ratio, 2),
                    "sample_extractions": matches[:4],
                }

    return None


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


@app.get("/health")
def health():
    return {"status": "ok"}
