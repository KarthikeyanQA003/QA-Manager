"""
Import test cases from Excel (.xlsx / .xlsm) or CSV.

Each sheet is read on its own. The header row is found automatically (the first
row that names a title-like column), and every column is matched to a test
case field by its header -- "Test Case Title", "Summary", "Test Steps",
"Expected Result", "Priority", "Module", ... -- which the user can change in
the import dialog before anything is written.

Rows without a title that carry steps / expected results continue the case
above them (the usual "one step per row" layout).
"""

import base64
import csv
import io
import re

FIELDS = {
    "ref": "External ID",
    "title": "Title",
    "module": "Module",
    "section": "Section",
    "priority": "Priority",
    "preconditions": "Preconditions",
    "steps": "Steps",
    "expected": "Expected result",
    "test_data": "Test data",
    "status": "Status",
    "notes": "Notes",
}

# Header synonyms, checked in order: exact match first, then "contains".
SYNONYMS = [
    ("ref", ["test case id", "testcase id", "tc id", "tc no", "tc #", "case id", "id", "test id", "reference",
             "ref"]),     # not "S.No": serial numbers restart on every sheet
    ("title", ["test case title", "test case name", "testcase name", "test title", "title", "test case",
               "testcase", "summary", "test scenario", "scenario", "name", "test case description",
               "description", "objective"]),
    ("module", ["module", "module name", "feature", "suite", "test suite", "component", "area", "functionality",
                "screen", "page"]),
    ("section", ["section", "sub module", "submodule", "sub-module", "sub feature", "category", "group"]),
    ("priority", ["priority", "severity", "importance"]),
    ("preconditions", ["preconditions", "pre conditions", "pre-conditions", "precondition", "pre-requisite",
                       "prerequisites", "prerequisite", "pre requisite"]),
    ("steps", ["test steps", "steps", "step", "steps to execute", "steps to reproduce", "procedure",
               "test procedure", "action", "actions", "step description"]),
    ("expected", ["expected result", "expected results", "expected", "expected outcome", "expected behaviour",
                  "expected behavior", "expected output"]),
    ("test_data", ["test data", "testdata", "data", "input data", "input"]),
    ("status", ["status", "result", "execution status", "test result", "pass/fail", "actual status"]),
    ("notes", ["notes", "comments", "comment", "remarks", "remark", "actual result", "actual"]),
]

PRIORITY_MAP = {"high": "High", "critical": "High", "blocker": "High", "urgent": "High", "p0": "High",
                "p1": "High", "1": "High", "major": "High", "medium": "Medium", "normal": "Medium",
                "moderate": "Medium", "p2": "Medium", "2": "Medium", "low": "Low", "minor": "Low",
                "trivial": "Low", "p3": "Low", "p4": "Low", "3": "Low", "4": "Low"}
STATUS_MAP = {"pass": "Pass", "passed": "Pass", "ok": "Pass", "fail": "Fail", "failed": "Fail",
              "blocked": "Blocked", "block": "Blocked", "retest": "Retest", "re-test": "Retest",
              "skip": "Skipped", "skipped": "Skipped", "na": "Skipped", "n/a": "Skipped",
              "not applicable": "Skipped", "not run": "Not Run", "no run": "Not Run", "not executed": "Not Run",
              "not started": "Not Run", "pending": "Not Run", "todo": "Not Run", "untested": "Not Run"}

MAX_ROWS = 20000


def _norm(h):
    return re.sub(r"\s+", " ", re.sub(r"[_\-:*()#./]+", " ", str(h or "").lower())).strip()


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if hasattr(v, "strftime"):
        return v.strftime("%d-%m-%Y")
    return str(v).replace("\r\n", "\n").strip()


def read_tables(name, content_b64):
    """[(sheet name, [[cell, ...], ...])] from an uploaded file."""
    try:
        raw = base64.b64decode(content_b64 or "", validate=False)
    except ValueError:
        raise ValueError("The file could not be read")
    if not raw:
        raise ValueError("The file is empty")
    lower = (name or "").lower()
    if lower.endswith((".csv", ".txt")):
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
        return [(re.sub(r"\.[^.]+$", "", name or "Sheet1"), [r for r in csv.reader(io.StringIO(text), dialect)])]
    if lower.endswith(".xls"):
        raise ValueError("Old .xls files are not supported -- open it in Excel and Save As .xlsx")
    from openpyxl import load_workbook  # project dependency
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError(f"Not a readable Excel (.xlsx) file: {exc}")
    tables = []
    for ws in wb.worksheets:
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i > MAX_ROWS:
                break
            rows.append([_cell(v) for v in row])
        tables.append((ws.title, rows))
    wb.close()
    return tables


def guess_mapping(headers):
    """{column index: field} from header names."""
    names = [_norm(h) for h in headers]
    mapping, used = {}, set()
    for exact in (True, False):
        for field, words in SYNONYMS:
            if field in used:
                continue
            for w in words:
                hit = next((i for i, n in enumerate(names) if i not in mapping and n and
                            (n == w if exact else (len(w) > 3 and w in n))), None)
                if hit is not None:
                    mapping[hit] = field
                    used.add(field)
                    break
    return mapping


def find_header(rows):
    """Index of the header row: the first of the top 15 rows that maps a title."""
    for i, row in enumerate(rows[:15]):
        if sum(1 for c in row if c) >= 2 and "title" in guess_mapping(row).values():
            return i
    for i, row in enumerate(rows[:15]):
        if sum(1 for c in row if c) >= 2:
            return i
    return None


def preview(name, content_b64):
    sheets = []
    for sheet, rows in read_tables(name, content_b64):
        h = find_header(rows)
        if h is None:
            continue
        headers = [c or f"Column {i + 1}" for i, c in enumerate(rows[h])]
        while headers and headers[-1].startswith("Column ") and not any(
                len(r) >= len(headers) and r[len(headers) - 1] for r in rows[h + 1:]):
            headers.pop()
        data = [r for r in rows[h + 1:] if any(r)]
        mapping = guess_mapping(headers)
        sheets.append({"name": sheet, "header_row": h + 1, "headers": headers,
                       "mapping": {str(k): v for k, v in mapping.items()},
                       "rows": len(data), "sample": [r[:len(headers)] for r in data[:6]],
                       "cases": len(parse_rows(data, mapping, sheet))})
    if not sheets:
        raise ValueError("No table with a header row was found in the file")
    return {"sheets": sheets, "fields": FIELDS}


def parse_rows(data, mapping, sheet_name, default_module=None):
    """Test case dicts from data rows; title-less rows continue the case above."""
    mapping = {int(k): v for k, v in mapping.items() if v in FIELDS}
    cases = []
    carry = {}          # merged cells: a blank module / section repeats the one above
    for row in data:
        rec = {}
        for idx, field in mapping.items():
            v = row[idx] if idx < len(row) else ""
            if v:
                rec[field] = (rec[field] + "\n" + v) if field in rec else v
        if not rec:
            continue
        if not rec.get("title"):
            if cases and any(rec.get(f) for f in ("steps", "expected", "test_data", "preconditions", "notes")):
                prev = cases[-1]
                for f in ("steps", "expected", "test_data", "preconditions", "notes"):
                    if rec.get(f):
                        prev[f] = (prev.get(f, "") + "\n" + rec[f]).strip()
            continue
        for f in ("module", "section"):
            if rec.get(f):
                carry[f] = rec[f]
            elif f in mapping.values() and carry.get(f):
                rec[f] = carry[f]
        if not rec.get("module"):
            rec["module"] = default_module if default_module is not None else sheet_name
        if rec.get("priority"):
            rec["priority"] = PRIORITY_MAP.get(rec["priority"].strip().lower(), "Medium")
        if rec.get("status"):
            rec["status"] = STATUS_MAP.get(rec["status"].strip().lower(), "Not Run")
        rec["title"] = rec["title"].split("\n")[0][:500]
        cases.append(rec)
    return cases


def cases_from_upload(name, content_b64, sheets):
    """sheets: [{name, mapping, module}] as confirmed in the dialog."""
    tables = dict(read_tables(name, content_b64))
    out = []
    for s in sheets:
        rows = tables.get(s.get("name"))
        if rows is None:
            continue
        h = max(0, int(s.get("header_row") or 1) - 1)
        data = [r for r in rows[h + 1:] if any(r)]
        module = (s.get("module") or "").strip() or None
        out += parse_rows(data, s.get("mapping") or {}, s["name"], module)
    return out
