"""
Files attached to test cases: screenshots, logs, documents, videos.

An attachment belongs to a case and, optionally, to the manual test run
(test_run_id) or automation run (run_id) it came from -- so a failure
screenshot recorded during TR-3 shows in that run and on the case. Files live
in data/attachments/; the database keeps the metadata.
"""

import base64
import mimetypes
import re
import shutil
from pathlib import Path

MAX_BYTES = 25 * 1024 * 1024
ALLOWED = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pdf", ".txt", ".log", ".csv", ".json", ".xml",
           ".html", ".htm", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".zip", ".mp4", ".webm", ".mov", ".har",
           ".md", ".eml", ".msg"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
# Shown in the browser; everything else downloads. HTML/SVG never render
# inline -- they could run script on this origin.
INLINE = IMAGES | {".pdf", ".mp4", ".webm", ".mov"}
TEXT = {".txt", ".log", ".csv", ".json", ".xml", ".md", ".har"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER, case_id INTEGER,
    test_run_id INTEGER, run_id INTEGER,
    name TEXT, mime TEXT, size INTEGER, path TEXT,
    source TEXT, uploaded_by TEXT, uploaded_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_attachments_case ON attachments (case_id);
"""


def safe_name(name):
    name = Path(str(name or "file")).name
    name = re.sub(r"[^\w.\- ()\[\]]+", "_", name).strip(" .") or "file"
    return name[-120:]


def _store(root, att_id, name):
    folder = Path(root)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{att_id}_{name}"


def add(conn, root, ws_id, case_id, name, content_b64, user, stamp, test_run_id=None, run_id=None,
        source="upload"):
    name = safe_name(name)
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED:
        raise ValueError(f"{name}: this file type can't be attached (allowed: images, PDF, text/logs, "
                         "Office files, zip, videos)")
    try:
        data = base64.b64decode(content_b64 or "", validate=False)
    except ValueError:
        raise ValueError(f"{name}: could not be read")
    if not data:
        raise ValueError(f"{name} is empty")
    if len(data) > MAX_BYTES:
        raise ValueError(f"{name} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    att_id = _insert(conn, ws_id, case_id, name, len(data), user, stamp, test_run_id, run_id, source)
    path = _store(root, att_id, name)
    path.write_bytes(data)
    conn.execute("UPDATE attachments SET path=? WHERE id=?", (str(path), att_id))
    return get(conn, att_id)


def add_file(conn, root, ws_id, case_id, src, user, stamp, run_id=None, source="automation"):
    """Copy a file from disk (an automation failure screenshot)."""
    src = Path(src)
    name = safe_name(src.name)
    if src.suffix.lower() not in ALLOWED or not src.is_file() or src.stat().st_size > MAX_BYTES:
        return None
    if conn.execute("SELECT 1 FROM attachments WHERE case_id=? AND run_id IS ? AND name=?",
                    (case_id, run_id, name)).fetchone():
        return None
    att_id = _insert(conn, ws_id, case_id, name, src.stat().st_size, user, stamp, None, run_id, source)
    path = _store(root, att_id, name)
    shutil.copyfile(src, path)
    conn.execute("UPDATE attachments SET path=? WHERE id=?", (str(path), att_id))
    return att_id


def _insert(conn, ws_id, case_id, name, size, user, stamp, test_run_id, run_id, source):
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return conn.execute(
        """INSERT INTO attachments (workspace_id, case_id, test_run_id, run_id, name, mime, size, source,
                                    uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (ws_id, case_id, test_run_id, run_id, name, mime, size, source, user, stamp)).lastrowid


def get(conn, att_id):
    row = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    return _public(dict(row)) if row else None


def _public(a):
    ext = Path(a["name"]).suffix.lower()
    a["is_image"] = ext in IMAGES
    a.pop("path", None)
    return a


def for_case(conn, case_id):
    return [_public(dict(r)) for r in conn.execute(
        "SELECT * FROM attachments WHERE case_id=? ORDER BY id DESC", (case_id,))]


def for_test_run(conn, test_run_id):
    out = {}
    for r in conn.execute("SELECT * FROM attachments WHERE test_run_id=? ORDER BY id", (test_run_id,)):
        out.setdefault(r["case_id"], []).append(_public(dict(r)))
    return out


def for_run(conn, run_id):
    return [_public(dict(r)) for r in conn.execute(
        "SELECT * FROM attachments WHERE run_id=? ORDER BY id", (run_id,))]


def file_for(conn, ws_id, att_id):
    """(bytes, content type, filename, inline) for download."""
    row = conn.execute("SELECT * FROM attachments WHERE id=? AND workspace_id=?", (att_id, ws_id)).fetchone()
    if not row or not row["path"] or not Path(row["path"]).is_file():
        return None
    ext = Path(row["name"]).suffix.lower()
    ctype = "text/plain; charset=utf-8" if ext in TEXT else (
        row["mime"] if ext in INLINE else "application/octet-stream")
    return Path(row["path"]).read_bytes(), ctype, row["name"], ext in INLINE or ext in TEXT


def delete(conn, ws_id, att_id):
    row = conn.execute("SELECT * FROM attachments WHERE id=? AND workspace_id=?", (att_id, ws_id)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM attachments WHERE id=?", (att_id,))
    if row["path"]:
        Path(row["path"]).unlink(missing_ok=True)
    return {"ok": True}


def delete_for_case(conn, case_id):
    for r in conn.execute("SELECT path FROM attachments WHERE case_id=?", (case_id,)).fetchall():
        if r["path"]:
            Path(r["path"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM attachments WHERE case_id=?", (case_id,))
