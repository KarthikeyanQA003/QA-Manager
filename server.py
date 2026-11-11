"""
CFlow Test Case Manager -- local web tool.

Keeps manual test cases in a local SQLite file, links each case to the
automation tests that cover it -- in any connected project
(Playwright JS/TS, Playwright Python, pytest, Selenium Java/Maven) -- triggers
those tests, and writes the run's pass/fail back onto every linked case.

    uv run python tools/test_case_manager/server.py            # http://localhost:8765
    uv run python tools/test_case_manager/server.py --port 9000
    uv run python tools/test_case_manager/server.py --import-junit results.xml [--project 2]

--import-junit records a run made outside the tool (CI, run_tests.py with
--junitxml, a Playwright/Maven run) and updates the linked cases the same way,
then exits.

Standard library only -- no extra dependencies.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))  # so the sibling modules import from any working directory
import auth  # noqa: E402
import codegen  # noqa: E402
import frameworks  # noqa: E402
import report  # noqa: E402
import testruns  # noqa: E402
import attachments  # noqa: E402
import importer  # noqa: E402

DATA_DIR = TOOL_DIR / "data"
RUNS_DIR = DATA_DIR / "runs"
DB_PATH = DATA_DIR / "tcm.db"
CONFIG_PATH = DATA_DIR / "config.json"
ATTACH_DIR = DATA_DIR / "attachments"
MAX_BODY = 60 * 1024 * 1024   # JSON body: an Excel file or a few screenshots, base64

# Set by --dev-user: enables a sign-in without Google for that one address,
# for local testing only. Off unless the server is started with the flag.
DEV_USER = None
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
ALLOWED_HOSTS = set()     # extra addresses from config.json "allowed_hosts" (hosting)

CASE_STATUSES = ["Not Run", "Running", "Pass", "Fail", "Blocked", "Retest", "Skipped"]

# Every time the tool records is India Standard Time, whatever the machine's zone.
IST = timezone(timedelta(hours=5, minutes=30), "IST")
PRIORITIES = ["High", "Medium", "Low"]

# Project 1's node ids are its plain test ids (kept from when the tool only
# knew the CFlow repo); every other project's are prefixed "p<id>:" so ids
# never collide across projects.
FIRST_PROJECT_ID = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    framework TEXT NOT NULL,
    path TEXT NOT NULL,
    scan_dir TEXT DEFAULT '',
    command TEXT DEFAULT '',
    created_at TEXT,
    last_scan_at TEXT
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    module TEXT DEFAULT '',
    priority TEXT DEFAULT 'Medium',
    preconditions TEXT DEFAULT '',
    steps TEXT DEFAULT '',
    expected TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    status TEXT DEFAULT 'Not Run',
    last_run_id INTEGER,
    last_run_at TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS automation_tests (
    node_id TEXT PRIMARY KEY,
    file TEXT, suite TEXT, cls TEXT, name TEXT, line INTEGER,
    doc TEXT, markers TEXT,
    junit_key TEXT,
    present INTEGER DEFAULT 1,
    last_result TEXT, last_duration REAL, last_message TEXT,
    last_run_id INTEGER, last_run_at TEXT
);
CREATE TABLE IF NOT EXISTS links (
    case_id INTEGER, node_id TEXT,
    PRIMARY KEY (case_id, node_id)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, targets TEXT, options TEXT,
    status TEXT, exit_code INTEGER,
    started_at TEXT, finished_at TEXT,
    passed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
    log_path TEXT, junit_path TEXT
);
CREATE TABLE IF NOT EXISTS run_results (
    run_id INTEGER, node_id TEXT, outcome TEXT, duration REAL, message TEXT,
    started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS case_history (
    case_id INTEGER, run_id INTEGER, status TEXT, at TEXT, source TEXT
);
"""


def now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def ist_from_ts(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M:%S")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def init_db():
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        # Columns added after the first release.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(automation_tests)")}
        if "project_id" not in have:
            conn.execute(f"ALTER TABLE automation_tests ADD COLUMN project_id INTEGER DEFAULT {FIRST_PROJECT_ID}")
        if "target" not in have:
            conn.execute("ALTER TABLE automation_tests ADD COLUMN target TEXT")
            conn.execute("UPDATE automation_tests SET target = node_id")

        # Sign-in and Jira-style projects (workspaces). Cases, automation
        # connections and runs each belong to one workspace.
        conn.executescript(auth.AUTH_SCHEMA)
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "password_hash" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        if "is_admin" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        conn.executescript(testruns.SCHEMA)
        conn.executescript(attachments.SCHEMA)
        conn.execute("""CREATE TABLE IF NOT EXISTS modules (
                            workspace_id INTEGER, module TEXT, section TEXT DEFAULT '', created_at TEXT,
                            PRIMARY KEY (workspace_id, module, section))""")
        case_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cases)")}
        if "ref" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN ref TEXT DEFAULT ''")
        if "test_data" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN test_data TEXT DEFAULT ''")
        if "section" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN section TEXT DEFAULT ''")
            # Automated cases start in a section named after their test class.
            for r in conn.execute(
                    """SELECT l.case_id, MIN(a.cls) cls FROM links l JOIN automation_tests a ON a.node_id = l.node_id
                       WHERE a.cls != '' GROUP BY l.case_id""").fetchall():
                conn.execute("UPDATE cases SET section=? WHERE id=?", (section_from_cls(r["cls"]), r["case_id"]))
        rr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(run_results)")}
        for col in ("started_at", "finished_at"):
            if col not in rr_cols:
                conn.execute(f"ALTER TABLE run_results ADD COLUMN {col} TEXT")
        for table in ("cases", "projects", "runs"):
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "workspace_id" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id INTEGER")
        orphans = sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE workspace_id IS NULL").fetchone()[0]
                      for t in ("cases", "projects", "runs"))
        if orphans:
            # Data from before workspaces existed becomes the "CFlow" project;
            # its first visitor becomes the owner (see auth.get_workspace).
            ws = conn.execute("SELECT id FROM workspaces WHERE key = 'CFL'").fetchone()
            ws_id = ws[0] if ws else conn.execute(
                """INSERT INTO workspaces (key, name, description, app_url, environments, members,
                                           owner, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                ("CFL", "CFlow", "CFlow workflow management -- end-to-end UI regression",
                 "https://testapp.cflowapps.com", json.dumps(["Test", "AP", "ME", "US", "EU"]), "[]",
                 None, now(), now())).lastrowid
            for t in ("cases", "projects", "runs"):
                conn.execute(f"UPDATE {t} SET workspace_id = ? WHERE workspace_id IS NULL", (ws_id,))
        # A server killed mid-run leaves its run 'Running' forever.
        conn.execute(
            "UPDATE runs SET status='Aborted', finished_at=? WHERE status IN ('Running','Queued')",
            (now(),),
        )
        conn.execute("UPDATE run_results SET outcome='not run' WHERE outcome IN ('queued','running')")
        conn.execute("UPDATE cases SET status='Not Run' WHERE status='Running'")
        # One title format inside the tool (format_title). Titles saved in other
        # formats before this existed are converted here; the old database is kept.
        changed = [(format_title(r["title"]), r["id"]) for r in conn.execute("SELECT id, title FROM cases")
                   if format_title(r["title"]) != r["title"]]
        # Modules, sections and automation suites follow the same format (format_name).
        renamed = [(format_name(r["module"]), format_name(r["section"]), r["id"])
                   for r in conn.execute("SELECT id, module, section FROM cases")
                   if (format_name(r["module"]), format_name(r["section"])) != (r["module"], r["section"])]
        suites = [(format_name(r["suite"]), r["node_id"])
                  for r in conn.execute("SELECT node_id, suite FROM automation_tests")
                  if format_name(r["suite"]) != r["suite"]]
        mods = [r for r in conn.execute("SELECT workspace_id, module, section, created_at FROM modules").fetchall()
                if (format_name(r["module"]), format_name(r["section"])) != (r["module"], r["section"])]
        if changed or renamed or suites or mods:
            backup = DATA_DIR / "tcm.backup-before-title-format.db"
            if not backup.exists():
                shutil.copy2(DB_PATH, backup)
            conn.executemany("UPDATE cases SET title=? WHERE id=?", changed)
            conn.executemany("UPDATE cases SET module=?, section=? WHERE id=?", renamed)
            conn.executemany("UPDATE automation_tests SET suite=? WHERE node_id=?", suites)
            for r in mods:
                conn.execute("DELETE FROM modules WHERE workspace_id=? AND module=? AND section=?",
                             (r["workspace_id"], r["module"], r["section"]))
                conn.execute("INSERT OR IGNORE INTO modules (workspace_id, module, section, created_at) VALUES (?,?,?,?)",
                             (r["workspace_id"], format_name(r["module"]), format_name(r["section"]), r["created_at"]))
            print(f"Name format: {len(changed)} title(s), {len(renamed)} case module/section(s), "
                  f"{len(suites)} automation suite(s) converted (backup: {backup.name})")


# ---------------------------------------------------------------------------
# Projects and automation discovery (static -- no imports, no node/mvn calls)
# ---------------------------------------------------------------------------

def get_project(conn, project_id):
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not p:
        raise ValueError(f"Unknown project {project_id}")
    return dict(p)


def node_prefix(project_id):
    return "" if project_id == FIRST_PROJECT_ID else f"p{project_id}:"


def scan_project(project_id):
    with db() as conn:
        p = get_project(conn, project_id)
    root = Path(p["path"])
    if not root.is_dir():
        raise ValueError(f"Project folder not found: {root}")
    prefix = "" if project_id == FIRST_PROJECT_ID else f"{p['name']}/"
    tests = frameworks.discover(p["framework"], root, p["scan_dir"] or "", prefix)
    pre = node_prefix(project_id)
    with db() as conn:
        conn.execute("UPDATE automation_tests SET present = 0 WHERE project_id = ?", (project_id,))
        for t in tests:
            t = dict(t, node_id=pre + t["target"] if p["framework"] != "selenium-maven"
                     else f"{pre}{t['file']}::{t['name']}", project_id=project_id)
            if p["framework"] == "playwright-js":
                # file:line shifts when lines move; key on the title path instead.
                t["node_id"] = f"{pre}{t['file']}::{t['cls'] + ' › ' if t['cls'] else ''}{t['name']}"
            t["suite"] = format_name(t["suite"])
            conn.execute(
                """INSERT INTO automation_tests (node_id, project_id, file, suite, cls, name, line, doc,
                                                 markers, junit_key, target, present)
                   VALUES (:node_id, :project_id, :file, :suite, :cls, :name, :line, :doc,
                           :markers, :junit_key, :target, 1)
                   ON CONFLICT(node_id) DO UPDATE SET project_id=excluded.project_id, file=excluded.file,
                     suite=excluded.suite, cls=excluded.cls, name=excluded.name, line=excluded.line,
                     doc=excluded.doc, markers=excluded.markers, junit_key=excluded.junit_key,
                     target=excluded.target, present=1""",
                t,
            )
        conn.execute("UPDATE projects SET last_scan_at=? WHERE id=?", (now(), project_id))
    return len(tests)


def sync_automation():
    with db() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM projects ORDER BY id")]
    total = 0
    for pid in ids:
        try:
            total += scan_project(pid)
        except ValueError as exc:
            print(f"[WARN] project {pid}: {exc}")
    return total


def section_from_cls(cls):
    """Test_AdminAddUser_Active -> 'Admin add user active' (see format_name)."""
    return format_name(re.sub(r"^Test_?", "", cls or "").strip() or (cls or ""))


_TITLE_PREFIX = re.compile(
    r"^(?:test_?(?:tc(?:[\s_\-]*[A-Za-z]+[\s_\-]+|[\s_\-]*))?\d+[A-Za-z]?|tc(?:[\s_\-]*[A-Za-z]+[\s_\-]+|[\s_\-]*)\d+[A-Za-z]?)"
    r"(?![A-Za-z0-9])[\s:._\-\u2013\u2014]*", re.I)


_ACRONYM = re.compile(r"^[A-Z][A-Z0-9]+$")          # OCR, CRUD, S1, OTP stay upper case
_NAME_SUFFIX = {"testcases", "testcase", "tests", "test", "cases", "case"}


def _words(s):
    """snake_case, kebab-case, camelCase and spaces -> a list of words."""
    s = re.sub(r"[_\-]+", " ", (s or "").strip())
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)          # viewAll -> view All
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)        # OCRGrid -> OCR Grid
    return s.split()


def sentence_case(s):
    """'AdminAccessPermissions ViewAll' -> 'Admin access permissions view all'; acronyms are kept."""
    out = " ".join(w if _ACRONYM.match(w) else w.lower() for w in _words(s))
    return out[:1].upper() + out[1:]


def format_title(title):
    """One title format inside the tool, whatever the IDE or spreadsheet used.

    The test number at the front is dropped (the tool has its own TC-0001 ids),
    snake_case and camelCase become words, and the text is sentence case:
      'TC01 FrgtPwdPrcss1'                    -> 'Frgt pwd prcss1'
      'tc_wfp_006 Verify benchmark hours'     -> 'Verify benchmark hours'
      'test_TC_WFS_003_form_creation'         -> 'Form creation'
      'select next dateformat'                -> 'Select next dateformat'
      'Verify OCR grid CRUD'                  -> 'Verify OCR grid CRUD'
    """
    s = (title or "").strip()
    s = _TITLE_PREFIX.sub("", s)
    s = _TITLE_PREFIX.sub("", s)                     # 'TC01 tc_rv_002 ...' carries two numbers
    if not s.strip():                                # the title was only a number: keep it
        s = (title or "").strip()
    return sentence_case(s)


def format_name(name):
    """Module, section and automation suite names, in the same format as titles.

    A trailing 'test cases' / 'tests' is dropped (every module here holds test cases):
      'Document_Designer_TestCases'            -> 'Document designer'
      'AdminAccessPermissions ViewAll EditMyItems' -> 'Admin access permissions view all edit my items'
      'OCR_testcases'                          -> 'OCR'
      'Frgt_pwd_test'                          -> 'Frgt pwd'
    """
    words = _words(name)
    trimmed = list(words)
    while trimmed and trimmed[-1].lower() in _NAME_SUFFIX:
        trimmed.pop()
    while trimmed and trimmed[0].lower() in _NAME_SUFFIX:       # Test_new_OCR -> New OCR
        trimmed.pop(0)
    return sentence_case(" ".join(trimmed or words))


def humanize(test_name):
    """test_TC_WFS_003_form_creation_main_section -> 'Form creation main section' (see format_title)."""
    if " " in test_name:          # already a sentence (Playwright / JUnit5 display names)
        return format_title(test_name)
    return format_title(re.sub(r"^test_?", "", test_name))


# ---------------------------------------------------------------------------
# Results -> automation tests -> linked cases
# ---------------------------------------------------------------------------

OUTCOME_RANK = {"failed": 3, "error": 3, "skipped": 1, "passed": 0}


def parse_junit(junit_paths, framework):
    """{junit_key: (outcome, duration, message)}; parametrized ids collapse onto
    their function, the worst outcome winning."""
    results = {}
    for junit_path in junit_paths:
        root = ET.parse(junit_path).getroot()
        for tc in root.iter("testcase"):
            key = frameworks.junit_key(framework, tc.get("classname", ""), tc.get("name", ""))
            outcome, message = "passed", ""
            for tag in ("failure", "error", "skipped"):
                el = tc.find(tag)
                if el is not None:
                    outcome = {"failure": "failed"}.get(tag, tag)
                    message = (el.get("message") or el.text or "").strip()[:2000]
                    break
            duration = float((tc.get("time") or "0").replace(",", "") or 0)
            prev = results.get(key)
            if prev is None or OUTCOME_RANK[outcome] > OUTCOME_RANK[prev[0]]:
                results[key] = (outcome, duration + (prev[1] if prev else 0), message)
            else:
                results[key] = (prev[0], prev[1] + duration, prev[2])
    return results


def case_status_from(outcomes):
    if any(o in ("failed", "error") for o in outcomes):
        return "Fail"
    if any(o == "skipped" for o in outcomes):
        return "Skipped" if not any(o == "passed" for o in outcomes) else "Blocked"
    return "Pass"


def apply_results(run_id, junit_paths, project_id=FIRST_PROJECT_ID, source="automation"):
    """Store a run's results and update every case linked to a test that ran."""
    with db() as conn:
        framework = get_project(conn, project_id)["framework"]
    results = parse_junit(junit_paths, framework)
    stamp = now()
    with db() as conn:
        key_to_node = {r["junit_key"]: r["node_id"] for r in conn.execute(
            "SELECT junit_key, node_id FROM automation_tests WHERE project_id=?", (project_id,))}
        counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
        ran = {}
        for key, (outcome, duration, message) in results.items():
            counts[outcome] += 1
            node_id = key_to_node.get(key)
            if not node_id:
                print(f"[WARN] result for unknown test {key} (re-scan the project?)")
                continue
            ran[node_id] = outcome
            upsert_result(conn, run_id, node_id, outcome, duration, message, stamp, final=True)
            conn.execute(
                """UPDATE automation_tests SET last_result=?, last_duration=?, last_message=?,
                   last_run_id=?, last_run_at=? WHERE node_id=?""",
                (outcome, duration, message, run_id, stamp, node_id),
            )

        updated = 0
        if ran:
            marks = ",".join("?" * len(ran))
            linked = conn.execute(
                f"SELECT case_id, node_id FROM links WHERE node_id IN ({marks})", list(ran)
            ).fetchall()
            per_case = {}
            for row in linked:
                per_case.setdefault(row["case_id"], []).append(ran[row["node_id"]])
            for case_id, outcomes in per_case.items():
                set_case_status(conn, case_id, run_id, case_status_from(outcomes), stamp, source)
                updated += 1
        recount_run(conn, run_id)
    return updated


def upsert_result(conn, run_id, node_id, outcome, duration, message, stamp, started_at=None, final=False):
    """One run_results row per test per run; the live feed and the final junit
    both write it, the junit having the last word on the result. `final` (the
    junit) keeps the finish time the live feed saw, which is the real one."""
    row = conn.execute("SELECT rowid, finished_at FROM run_results WHERE run_id=? AND node_id=?",
                       (run_id, node_id)).fetchone()
    if row:
        finished = row["finished_at"] if final and row["finished_at"] else stamp
        conn.execute("""UPDATE run_results SET outcome=?, duration=?, message=?, finished_at=?,
                        started_at=COALESCE(started_at, ?) WHERE rowid=?""",
                     (outcome, duration, message, finished, started_at, row["rowid"]))
    else:
        conn.execute("""INSERT INTO run_results (run_id, node_id, outcome, duration, message, started_at, finished_at)
                        VALUES (?,?,?,?,?,?,?)""",
                     (run_id, node_id, outcome, duration, message, started_at, stamp))


def set_case_status(conn, case_id, run_id, status, stamp, source="automation"):
    conn.execute("UPDATE cases SET status=?, last_run_id=?, last_run_at=?, updated_at=? WHERE id=?",
                 (status, run_id, stamp, stamp, case_id))
    if status == "Running":
        return
    last = conn.execute("SELECT status FROM case_history WHERE case_id=? AND run_id=? ORDER BY rowid DESC LIMIT 1",
                        (case_id, run_id)).fetchone()
    if not last or last["status"] != status:
        conn.execute("INSERT INTO case_history (case_id, run_id, status, at, source) VALUES (?,?,?,?,?)",
                     (case_id, run_id, status, stamp, source))


def recount_run(conn, run_id):
    conn.execute(
        """UPDATE runs SET
             passed  = (SELECT COUNT(*) FROM run_results WHERE run_id=:r AND outcome='passed'),
             failed  = (SELECT COUNT(*) FROM run_results WHERE run_id=:r AND outcome='failed'),
             skipped = (SELECT COUNT(*) FROM run_results WHERE run_id=:r AND outcome='skipped'),
             errors  = (SELECT COUNT(*) FROM run_results WHERE run_id=:r AND outcome='error')
           WHERE id=:r""", {"r": run_id})


def apply_live(run_id, events):
    """Live events [(node_id, kind, outcome, duration, message, ts)] -> run_results,
    automation_tests and the linked cases, while the run is still going."""
    with db() as conn:
        touched = set()
        for node_id, kind, outcome, duration, message, ts in events:
            at = ist_from_ts(ts) if ts else now()
            row = conn.execute("SELECT rowid, outcome FROM run_results WHERE run_id=? AND node_id=?",
                               (run_id, node_id)).fetchone()
            if kind in ("start", "rerun"):
                if not row:
                    conn.execute("INSERT INTO run_results (run_id, node_id, outcome, started_at) VALUES (?,?,?,?)",
                                 (run_id, node_id, "running", at))
                # A finished row starting again is the next parametrize variant
                # (or a rerun): back to running, unless it has already failed.
                elif row["outcome"] in ("queued", "not run", "passed", "skipped") or kind == "rerun":
                    conn.execute("""UPDATE run_results SET outcome='running', started_at=COALESCE(started_at, ?),
                                    finished_at=NULL WHERE rowid=?""", (at, row["rowid"]))
            else:
                # Parametrized variants share one node: keep the worst outcome.
                if row and row["outcome"] in OUTCOME_RANK and OUTCOME_RANK[row["outcome"]] > OUTCOME_RANK[outcome]:
                    outcome = row["outcome"]
                upsert_result(conn, run_id, node_id, outcome, duration, message, at, started_at=at)
                conn.execute("""UPDATE automation_tests SET last_result=?, last_duration=?, last_message=?,
                                last_run_id=?, last_run_at=? WHERE node_id=?""",
                             (outcome, duration, message, run_id, at, node_id))
            touched.add(node_id)
        if not touched:
            return
        marks = ",".join("?" * len(touched))
        case_ids = {r[0] for r in conn.execute(f"SELECT case_id FROM links WHERE node_id IN ({marks})", list(touched))}
        stamp = now()
        for case_id in case_ids:
            outs = [r[0] for r in conn.execute(
                """SELECT r.outcome FROM links l JOIN run_results r ON r.node_id = l.node_id AND r.run_id = ?
                   WHERE l.case_id = ?""", (run_id, case_id))]
            done = [o for o in outs if o in OUTCOME_RANK]
            pending = [o for o in outs if o in ("queued", "running")]
            if any(o in ("failed", "error") for o in done) or (done and not pending):
                status = case_status_from(done)          # a failure shows straight away
            elif "running" in outs or done:
                status = "Running"
            else:
                continue
            set_case_status(conn, case_id, run_id, status, stamp)
        recount_run(conn, run_id)


# ---------------------------------------------------------------------------
# Runner -- one run at a time; a run spanning projects runs them in turn
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.run_id = None
        self.active = False
        self.cancelled = set()

    def busy(self):
        return self.active

    def start(self, ws_id, title, node_ids, headless=False, region="Test", extra="", started_by=None):
        with self.lock:
            if self.active:
                raise RuntimeError(f"Run #{self.run_id} is still in progress -- one run at a time.")
            with db() as conn:
                marks = ",".join("?" * len(node_ids))
                tests = rows(conn.execute(
                    f"""SELECT a.*, p.framework, p.path, p.command, p.name AS project_name
                        FROM automation_tests a JOIN projects p ON p.id = a.project_id
                        WHERE a.present = 1 AND p.workspace_id = ? AND a.node_id IN ({marks})
                        ORDER BY a.project_id, a.file, a.line""",
                    [ws_id, *node_ids]))
                if not tests:
                    raise ValueError("None of the selected tests exist any more -- re-scan the project")
                options = {"headless": headless, "region": region, "extra": extra, "started_by": started_by}
                run_id = conn.execute(
                    """INSERT INTO runs (workspace_id, title, targets, options, status, started_at)
                       VALUES (?,?,?,?,?,?)""",
                    (ws_id, title, json.dumps([t["node_id"] for t in tests]), json.dumps(options),
                     "Running", now()),
                ).lastrowid
                log_path = RUNS_DIR / f"run_{run_id}.log"
                conn.execute("UPDATE runs SET log_path=? WHERE id=?", (str(log_path), run_id))
                self._reset_cases(conn, ws_id, run_id, [t["node_id"] for t in tests])
                conn.executemany("INSERT INTO run_results (run_id, node_id, outcome) VALUES (?,?,'queued')",
                                 [(run_id, t["node_id"]) for t in tests])

            groups = {}
            for t in tests:
                groups.setdefault(t["project_id"], []).append(t)
            self.active, self.run_id = True, run_id
            threading.Thread(target=self._run_all, args=(run_id, groups, options, log_path),
                             daemon=True).start()
            return run_id

    @staticmethod
    def _reset_cases(conn, ws_id, run_id, node_ids):
        """Every run starts from a clean slate: ALL of the project's cases go back
        to 'Not Run' (Dashboard and Board start fresh); the cases in this run then
        fill in as Pass / Fail / Skipped. Each case's previous result stays in its
        history."""
        stamp = now()
        changed = [r[0] for r in conn.execute(
            "SELECT id FROM cases WHERE workspace_id = ? AND status != 'Not Run'", (ws_id,))]
        conn.execute("UPDATE cases SET status='Not Run', updated_at=? WHERE workspace_id = ?", (stamp, ws_id))
        conn.executemany(
            "INSERT INTO case_history (case_id, run_id, status, at, source) VALUES (?,?,?,?,?)",
            [(cid, run_id, "Not Run", stamp, f"reset: run #{run_id} started") for cid in changed])
        return len(changed)

    def _run_all(self, run_id, groups, options, log_path):
        exit_codes, updated = [], 0
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as log:
                for project_id, tests in groups.items():
                    if run_id in self.cancelled:
                        break
                    code, n = self._run_project(run_id, project_id, tests, options, log)
                    exit_codes.append(code)
                    updated += n
        finally:
            cancelled = run_id in self.cancelled
            status = ("Cancelled" if cancelled else
                      "Passed" if exit_codes and all(c == 0 for c in exit_codes) else "Failed")
            with db() as conn:
                conn.execute("UPDATE runs SET status=?, exit_code=?, finished_at=? WHERE id=?",
                             (status, max(exit_codes) if exit_codes else None, now(), run_id))
                # Tests that never reported (cancelled, crashed, deselected).
                conn.execute("""UPDATE run_results SET outcome='not run', finished_at=COALESCE(finished_at, ?)
                                WHERE run_id=? AND outcome IN ('queued','running')""", (now(), run_id))
                ws = conn.execute("SELECT workspace_id FROM runs WHERE id=?", (run_id,)).fetchone()[0]
                conn.execute("UPDATE cases SET status='Not Run' WHERE workspace_id=? AND status='Running'", (ws,))
            try:
                write_report(run_id)
            except Exception as exc:  # the run itself is recorded; a report problem must not hide that
                print(f"[RUN {run_id}] report not written: {exc}")
            self.active, self.proc = False, None
            print(f"[RUN {run_id}] {status}; {updated} linked case(s) updated")

    def _run_project(self, run_id, project_id, tests, options, log):
        p = tests[0]
        framework = p["framework"]
        junit = RUNS_DIR / f"run_{run_id}_p{project_id}.xml"
        with db() as conn:
            universe = rows(conn.execute(
                "SELECT node_id, file, target FROM automation_tests WHERE project_id=? AND present=1",
                (project_id,)))
        for t in tests + universe:
            t["node_key"] = t["node_id"]
        cmd = frameworks.build_command(framework, p["command"], tests, junit,
                                       options["headless"], options["region"], universe)
        if options["extra"].strip():
            cmd += " " + options["extra"].strip()
        if os.name == "nt" and len(cmd) > 8000:
            log.write(f"[ERROR] {p['project_name']}: the command is {len(cmd)} characters -- over the "
                      "Windows limit (8191). Run the whole suite or fewer scattered tests.\n\n")
            return 1, 0
        log.write(f"=== {p['project_name']} ({frameworks.FRAMEWORKS[framework]['label']}) -- "
                  f"{len(tests)} test(s)\n$ {cmd}\n  (in {p['path']})\n\n")
        log.flush()

        live_file = RUNS_DIR / f"run_{run_id}_p{project_id}.live.jsonl"
        env = dict(project_env(), PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", FORCE_COLOR="0",
                   **frameworks.run_env(framework, junit, live_file))
        # CREATE_NO_WINDOW: never pop a console window for the run (output goes
        # to the run log). GUI windows -- the browser in headed mode -- still show.
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
        started = time.time()
        # shell=True: npx / mvn are .cmd scripts on Windows, and the command is a
        # user-editable template.
        self.proc = subprocess.Popen(cmd, shell=True, cwd=p["path"], stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=flags, env=env)
        feed = LiveFeed(run_id, project_id, framework, live_file, Path(log.name), p["path"])
        while True:
            try:
                code = self.proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                feed.poll()
        feed.poll()
        log.write(f"\n=== exit code {code}\n\n")
        log.flush()

        files = frameworks.result_files(framework, p["path"], junit, started)
        if not files:
            log.write("[WARN] no junit results were produced -- linked cases not updated\n\n")
            return code, 0
        try:
            return code, apply_results(run_id, files, project_id)
        except ET.ParseError as exc:
            log.write(f"[WARN] unreadable junit xml: {exc}\n\n")
            return code, 0

    def cancel(self, run_id):
        with self.lock:
            if not self.active or self.run_id != run_id:
                return False
            self.cancelled.add(run_id)
            proc = self.proc
            if proc and proc.poll() is None:
                if os.name == "nt":
                    # The run spawns browsers / JVMs; kill the whole tree.
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
                else:
                    proc.terminate()
            return True


def build_report(run_id):
    with db() as conn:
        return report.build(conn, run_id, now())


def write_report(run_id):
    path = RUNS_DIR / f"run_{run_id}_report.html"
    path.write_text(build_report(run_id), encoding="utf-8")
    return path


def live_progress(ws_id):
    """The running run's progress for the Dashboard / Board strip, or None."""
    if not RUNNER.busy():
        return None
    with db() as conn:
        run = conn.execute("SELECT id, title, started_at, options FROM runs WHERE id=? AND workspace_id=?",
                           (RUNNER.run_id, ws_id)).fetchone()
        if not run:
            return None
        counts = {r["outcome"]: r["n"] for r in conn.execute(
            "SELECT outcome, COUNT(*) n FROM run_results WHERE run_id=? GROUP BY outcome", (run["id"],))}
        now_running = [r["name"] for r in conn.execute(
            """SELECT COALESCE(a.name, r.node_id) name FROM run_results r
               LEFT JOIN automation_tests a ON a.node_id = r.node_id
               WHERE r.run_id=? AND r.outcome='running' ORDER BY r.started_at""", (run["id"],))]
    total = sum(counts.values())
    done = sum(counts.get(k, 0) for k in OUTCOME_RANK)
    return {"id": run["id"], "title": run["title"], "started_at": run["started_at"], "total": total, "done": done,
            "passed": counts.get("passed", 0), "failed": counts.get("failed", 0) + counts.get("error", 0),
            "skipped": counts.get("skipped", 0), "now_running": now_running}


def project_env():
    """The environment a project's tests run in: this tool's own virtualenv
    (when started with `uv run`) removed, so `python` / `pytest` in the run
    command are the project's, not the tool's."""
    env = dict(os.environ)
    if sys.prefix != sys.base_prefix:
        venv = os.path.normcase(os.path.abspath(sys.prefix))
        env.pop("VIRTUAL_ENV", None)
        env["PATH"] = os.pathsep.join(
            p for p in env.get("PATH", "").split(os.pathsep)
            if p and not os.path.normcase(os.path.abspath(p)).startswith(venv))
    return env


class LiveFeed:
    """Follows a running project's live output and records each test as it
    starts and finishes: the pytest plugin's JSON lines, or -- for Playwright
    JS -- the list reporter's result lines in the run log."""

    # "Screenshot saved: Reports\Screenshots\fail_x.png" (CFlow), "screenshot: x.png", ...
    SHOT = re.compile(r"(?i)screenshot(?: saved)?(?: at| to)?\s*[:=]\s*['\"]?(.+?\.(?:png|jpe?g|webp))['\"]?\s*$")
    NODE_LINE = re.compile(r"^(?:\[gw\d+\]\s+(?:\[\s*\d+%\]\s+)?\w+\s+)?(\S+\.py::\S+)")

    def __init__(self, run_id, project_id, framework, live_file, log_path, project_path=""):
        self.run_id, self.framework = run_id, framework
        self.root = Path(project_path or ".")
        self.current, self.shots, self.failed, self.attached = None, {}, set(), set()
        self.live_file, self.log_path = Path(live_file), Path(log_path)
        self.live_pos = self.log_pos = 0
        self.log_rest = ""
        with db() as conn:
            tests = rows(conn.execute(
                "SELECT node_id, target, junit_key FROM automation_tests WHERE project_id=?", (project_id,)))
        self.by_target = {t["target"].replace("\\", "/"): t["node_id"] for t in tests}
        self.by_junit = {t["junit_key"]: t["node_id"] for t in tests}

    def node_for(self, nodeid):
        nodeid = re.sub(r"\[.*\]$", "", nodeid.replace("\\", "/"))     # parametrize ids
        return self.by_target.get(nodeid)

    def poll(self):
        events = []
        try:
            if self.live_file.exists():
                with open(self.live_file, "rb") as fh:
                    fh.seek(self.live_pos)
                    chunk = fh.read()
                complete = chunk[:chunk.rfind(b"\n") + 1]       # leave a half-written line for next time
                self.live_pos += len(complete)
                for line in complete.decode("utf-8", errors="replace").splitlines():
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    node = self.node_for(e.get("nodeid", ""))
                    if node:
                        events.append((node, e["event"], e.get("outcome"), e.get("duration") or 0,
                                       e.get("message") or "", e.get("t")))
                        if e["event"] == "start":
                            self.current = node
                        if e.get("outcome") in ("failed", "error"):
                            self.failed.add(node)
            if self.log_path.exists():
                with open(self.log_path, "rb") as fh:
                    fh.seek(self.log_pos)
                    chunk = fh.read()
                self.log_pos += len(chunk)
                text = self.log_rest + chunk.decode("utf-8", errors="replace")
                lines = text.split("\n")
                self.log_rest = lines.pop()
                for line in lines:
                    line = line.rstrip("\r")
                    hit = frameworks.parse_live_line(self.framework, line)
                    if hit and hit[0] in self.by_junit:
                        events.append((self.by_junit[hit[0]], "result", hit[1], 0, "", time.time()))
                        if hit[1] == "failed":
                            self.failed.add(self.by_junit[hit[0]])
                    m = self.NODE_LINE.match(line)
                    if m and self.node_for(m.group(1)):
                        self.current = self.node_for(m.group(1))
                    shot = self.SHOT.search(line)
                    if shot and self.current:
                        self.shots.setdefault(self.current, []).append(shot.group(1).strip())
            if events:
                apply_live(self.run_id, events)
            self.attach_failure_shots()
        except (OSError, sqlite3.Error) as exc:
            print(f"[RUN {self.run_id}] live update skipped: {exc}")

    def attach_failure_shots(self):
        """Copy the screenshots a failed test saved onto its linked test cases."""
        todo = [(n, s) for n in self.failed for s in self.shots.get(n, []) if (n, s) not in self.attached]
        if not todo:
            return
        with db() as conn:
            ws_id = conn.execute("SELECT workspace_id FROM runs WHERE id=?", (self.run_id,)).fetchone()[0]
            for node, shot in todo:
                self.attached.add((node, shot))
                path = Path(shot) if Path(shot).is_absolute() else self.root / shot
                for r in conn.execute("SELECT case_id FROM links WHERE node_id=?", (node,)).fetchall():
                    attachments.add_file(conn, ATTACH_DIR, ws_id, r["case_id"], path, "automation", now(),
                                         run_id=self.run_id, source=f"automation run #{self.run_id}")


RUNNER = Runner()

SENT = object()   # a handler already wrote its own response (an HTML page)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

CASE_FIELDS = ["ref", "title", "module", "section", "priority", "preconditions", "steps", "expected", "test_data",
               "notes", "status"]


def case_row(conn, case_id):
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        return None
    case = dict(case)
    case["links"] = rows(conn.execute(
        """SELECT a.node_id, a.file, a.line, a.suite, a.name, a.present, a.last_result, a.last_run_at,
                  p.path AS root, p.name AS project, p.framework
           FROM links l JOIN automation_tests a ON a.node_id = l.node_id
           LEFT JOIN projects p ON p.id = a.project_id
           WHERE l.case_id=? ORDER BY a.node_id""", (case_id,)))
    case["history"] = rows(conn.execute(
        "SELECT run_id, status, at, source FROM case_history WHERE case_id=? ORDER BY rowid DESC LIMIT 30",
        (case_id,)))
    case["attachments"] = attachments.for_case(conn, case_id)
    return case


def list_cases(q, ws_id):
    sql = """SELECT c.*, (SELECT COUNT(*) FROM links l WHERE l.case_id = c.id) AS link_count,
                    (SELECT COUNT(*) FROM attachments x WHERE x.case_id = c.id) AS attachment_count
             FROM cases c WHERE c.workspace_id = ?"""
    args = [ws_id]
    if q.get("q"):
        sql += """ AND (c.title LIKE ? OR c.steps LIKE ? OR c.expected LIKE ? OR c.test_data LIKE ?
                   OR c.ref LIKE ? OR ('TC-' || printf('%04d', c.id)) LIKE ?)"""
        like = f"%{q['q']}%"
        args += [like] * 6
    if "section" in q:
        sql += " AND c.section = ?"
        args.append(q["section"])
    for field in ("module", "status", "priority"):
        if q.get(field):
            sql += f" AND c.{field} = ?"
            args.append(q[field])
    if q.get("linked") == "yes":
        sql += " AND EXISTS (SELECT 1 FROM links l WHERE l.case_id = c.id)"
    elif q.get("linked") == "no":
        sql += " AND NOT EXISTS (SELECT 1 FROM links l WHERE l.case_id = c.id)"
    sql += " ORDER BY c.id DESC"
    with db() as conn:
        return rows(conn.execute(sql, args))


def list_automation(q, ws_id):
    sql = """SELECT a.*, p.path AS root, p.name AS project, p.framework,
                    (SELECT GROUP_CONCAT(l.case_id) FROM links l WHERE l.node_id = a.node_id) AS case_ids
             FROM automation_tests a JOIN projects p ON p.id = a.project_id
             WHERE a.present = 1 AND p.workspace_id = ?"""
    args = [ws_id]
    if q.get("q"):
        sql += " AND (a.node_id LIKE ? OR a.doc LIKE ? OR a.name LIKE ?)"
        args += [f"%{q['q']}%"] * 3
    if q.get("project"):
        sql += " AND a.project_id = ?"
        args.append(int(q["project"]))
    if q.get("suite"):
        sql += " AND a.suite = ?"
        args.append(q["suite"])
    if q.get("result"):
        if q["result"] == "none":
            sql += " AND a.last_result IS NULL"
        else:
            sql += " AND a.last_result = ?"
            args.append(q["result"])
    if q.get("linked") == "no":
        sql += " AND NOT EXISTS (SELECT 1 FROM links l WHERE l.node_id = a.node_id)"
    sql += " ORDER BY a.project_id, a.file, a.line"
    with db() as conn:
        return rows(conn.execute(sql, args))


def list_projects(ws_id):
    """Automation connections of one workspace."""
    with db() as conn:
        out = rows(conn.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM automation_tests a WHERE a.project_id = p.id AND a.present = 1) AS tests,
                      (SELECT COUNT(*) FROM automation_tests a WHERE a.project_id = p.id AND a.present = 1
                         AND EXISTS (SELECT 1 FROM links l WHERE l.node_id = a.node_id)) AS linked
               FROM projects p WHERE p.workspace_id = ? ORDER BY p.id""", (ws_id,)))
    for p in out:
        p["label"] = frameworks.FRAMEWORKS.get(p["framework"], {}).get("label", p["framework"])
        p["default_command"] = frameworks.FRAMEWORKS.get(p["framework"], {}).get("command", "")
        p["exists"] = Path(p["path"]).is_dir()
    return out


def owned_project(conn, project_id, ws_id):
    p = get_project(conn, project_id)
    if p["workspace_id"] != ws_id:
        raise ValueError(f"Unknown automation connection {project_id}")
    return p


def save_project(project_id, data, ws_id):
    name = (data.get("name") or "").strip()
    framework = data.get("framework")
    path = (data.get("path") or "").strip().strip('"')
    if not name:
        raise ValueError("Connection name is required")
    if framework not in frameworks.FRAMEWORKS:
        raise ValueError("Pick a framework")
    if not Path(path).is_dir():
        raise ValueError(f"Folder not found: {path}")
    values = (name, framework, str(Path(path).resolve()), (data.get("scan_dir") or "").strip(),
              (data.get("command") or "").strip())
    with db() as conn:
        if project_id is None:
            project_id = conn.execute(
                """INSERT INTO projects (workspace_id, name, framework, path, scan_dir, command, created_at)
                   VALUES (?,?,?,?,?,?,?)""", (ws_id, *values, now())).lastrowid
        else:
            owned_project(conn, project_id, ws_id)
            conn.execute("UPDATE projects SET name=?, framework=?, path=?, scan_dir=?, command=? WHERE id=?",
                         (*values, project_id))
    return {"id": project_id, "tests": scan_project(project_id)}


def delete_project(project_id, ws_id):
    with db() as conn:
        owned_project(conn, project_id, ws_id)
        nodes = [r[0] for r in conn.execute("SELECT node_id FROM automation_tests WHERE project_id=?", (project_id,))]
        conn.executemany("DELETE FROM links WHERE node_id=?", [(n,) for n in nodes])
        conn.execute("DELETE FROM automation_tests WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return {"ok": True, "unlinked_tests": len(nodes)}


def summary(ws_id):
    with db() as conn:
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM cases WHERE workspace_id = ? GROUP BY status", (ws_id,))}
        modules = rows(conn.execute(
            """SELECT module,
                      COUNT(*) total,
                      SUM(status='Pass') pass, SUM(status='Fail') fail,
                      SUM(status IN ('Blocked','Skipped','Retest')) blocked, SUM(status='Not Run') notrun
               FROM cases WHERE workspace_id = ? GROUP BY module ORDER BY module""", (ws_id,)))
        auto = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(EXISTS (SELECT 1 FROM links l WHERE l.node_id = a.node_id)) linked
               FROM automation_tests a JOIN projects p ON p.id = a.project_id
               WHERE a.present = 1 AND p.workspace_id = ?""", (ws_id,)).fetchone()
        cases_total = conn.execute("SELECT COUNT(*) FROM cases WHERE workspace_id = ?", (ws_id,)).fetchone()[0]
        cases_linked = conn.execute(
            """SELECT COUNT(DISTINCT l.case_id) FROM links l JOIN cases c ON c.id = l.case_id
               WHERE c.workspace_id = ?""", (ws_id,)).fetchone()[0]
        runs = rows(conn.execute("SELECT * FROM runs WHERE workspace_id = ? ORDER BY id DESC LIMIT 5", (ws_id,)))
        sections = rows(conn.execute(
            """SELECT module, section, COUNT(*) total FROM cases WHERE workspace_id = ?
               GROUP BY module, section ORDER BY module, section""", (ws_id,)))
        have_mod = {m["module"] for m in modules}
        have_sec = {(x["module"], x["section"]) for x in sections}
        for r in conn.execute("SELECT module, section FROM modules WHERE workspace_id=? ORDER BY module, section",
                              (ws_id,)).fetchall():
            if r["module"] not in have_mod:
                modules.append({"module": r["module"], "total": 0, "pass": 0, "fail": 0, "blocked": 0, "notrun": 0})
                have_mod.add(r["module"])
            if r["section"] and (r["module"], r["section"]) not in have_sec:
                sections.append({"module": r["module"], "section": r["section"], "total": 0})
        modules.sort(key=lambda m: (m["module"] or "").lower())
        sections.sort(key=lambda x: ((x["module"] or "").lower(), (x["section"] or "").lower()))
        suites = [r[0] for r in conn.execute(
            """SELECT DISTINCT a.suite FROM automation_tests a JOIN projects p ON p.id = a.project_id
               WHERE a.present = 1 AND p.workspace_id = ? ORDER BY a.suite""", (ws_id,))]
    return {
        "by_status": by_status, "modules": modules, "sections": sections, "cases_total": cases_total,
        "results": testruns.RESULTS,
        "cases_linked": cases_linked, "auto_total": auto["total"], "auto_linked": auto["linked"] or 0,
        "recent_runs": runs, "suites": suites, "statuses": CASE_STATUSES, "priorities": PRIORITIES,
        "running": RUNNER.run_id if RUNNER.busy() else None,
        "live": live_progress(ws_id),
        "frameworks": {k: v["label"] for k, v in frameworks.FRAMEWORKS.items()},
        "framework_commands": {k: v["command"] for k, v in frameworks.FRAMEWORKS.items()},
        "projects": [{"id": p["id"], "name": p["name"], "framework": p["framework"]} for p in list_projects(ws_id)],
    }


EXPORT_COLUMNS = ["ID", "External ID", "Title", "Module", "Section", "Priority", "Status", "Preconditions", "Steps",
                  "Expected Result", "Test Data", "Notes", "Automation Tests", "Last Run", "Created", "Updated"]


def cases_with_links(ws_id):
    with db() as conn:
        cases = rows(conn.execute("SELECT * FROM cases WHERE workspace_id = ? ORDER BY module, id", (ws_id,)))
        links = {}
        for r in conn.execute(
                """SELECT l.case_id, l.node_id FROM links l JOIN cases c ON c.id = l.case_id
                   WHERE c.workspace_id = ? ORDER BY l.node_id""", (ws_id,)):
            links.setdefault(r["case_id"], []).append(r["node_id"])
    for c in cases:
        c["links"] = links.get(c["id"], [])
    return cases


def export_rows(ws_id):
    return [[f"TC-{c['id']:04d}", c["ref"] or "", c["title"], c["module"], c["section"], c["priority"], c["status"],
             c["preconditions"], c["steps"], c["expected"], c["test_data"], c["notes"],
             "\n".join(c["links"]), c["last_run_at"] or "", c["created_at"], c["updated_at"]]
            for c in cases_with_links(ws_id)]


def export_file(fmt, ws_id, ws_key="cflow"):
    """(bytes, content type, filename) for all test cases of a workspace."""
    import csv
    import io
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    prefix = re.sub(r"[^0-9a-z]+", "_", ws_key.lower()) or "project"
    if fmt in codegen.FORMATS:
        root = f"{prefix}_{fmt.replace('-', '_')}_{stamp}"
        return codegen.build_zip(fmt, cases_with_links(ws_id), root), "application/zip", f"{root}.zip"
    data = export_rows(ws_id)
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(EXPORT_COLUMNS)
        writer.writerows(data)
        # BOM so Excel opens the UTF-8 file with the right encoding.
        return ("﻿" + buf.getvalue()).encode("utf-8"), "text/csv", f"{prefix}_test_cases_{stamp}.csv"

    from openpyxl import Workbook  # project dependency
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Test Cases"
    ws.append(EXPORT_COLUMNS)
    for row in data:
        ws.append(row)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2F6FDE")
    widths = [10, 14, 50, 26, 26, 10, 10, 30, 60, 40, 30, 30, 70, 19, 19, 19]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return (buf.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            f"{prefix}_test_cases_{stamp}.xlsx")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(TOOL_DIR / "static"), **kwargs)

    def log_message(self, fmt, *args):
        pass  # keep the console for run events

    # -- helpers
    def send_json(self, data, code=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError(f"Upload too large (over {MAX_BODY // (1024 * 1024)} MB)")
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def request_allowed(self, method):
        """Only this tool's own page may call the API.

        Runs execute shell commands, so a request from any other site open in the
        browser (CSRF to localhost) or a DNS-rebound hostname must be refused:
          - Host must be localhost / 127.0.0.1 (blocks DNS rebinding)
          - Origin, when sent, must be this server (blocks cross-site fetches)
          - writes must be JSON, which a cross-site <form> cannot send and a
            cross-site fetch cannot send without a CORS preflight we never grant
        """
        host_header = (self.headers.get("Host") or "").strip()
        host = urlparse("//" + host_header).hostname or ""
        if host not in LOCAL_HOSTS and host not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin:
            # The page and the API share one address, so Origin must name the same
            # host:port the browser asked for (works directly and behind a proxy).
            if urlparse(origin).netloc.lower() != host_header.lower():
                return False
        if method != "GET" and not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return False
        return True

    def route(self, method):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            if method == "GET":
                return super().do_GET()
            return self.send_json({"error": "not found"}, 404)
        if not self.request_allowed(method):
            return self.send_json({"error": "forbidden: requests must come from the Test Case Manager page"}, 403)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = url.path.strip("/").split("/")[1:]
        self.cookie_out = None
        try:
            if parts[:1] in (["auth"], ["setup"]):
                result = self.dispatch_auth(method, parts, q)
            else:
                self.user = self.current_user()
                if parts[:1] == ["workspaces"]:
                    result = self.dispatch_workspaces(method, parts[1:])
                else:
                    self.ws = self.current_workspace(q)
                    if parts == ["export"] and method == "GET":
                        return self.send_file(*export_file(q.get("format", "xlsx"), self.ws["id"], self.ws["key"]))
                    result = self.dispatch(method, parts, q)
            if result is SENT:
                return None
            if result is None:
                return self.send_json({"error": "not found"}, 404)
            self.send_json(result)
        except auth.AuthError as exc:
            self.send_json({"error": str(exc), "auth": True}, exc.status)
        except (ValueError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:  # surface server bugs to the UI instead of hanging
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def send_file(self, body, ctype, name):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, download=False, name="report.html"):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Disposition", f'{"attachment" if download else "inline"}; filename="{name}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if getattr(self, "cookie_out", None):
            self.send_header("Set-Cookie", self.cookie_out)
        super().end_headers()

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")

    # -- sign-in
    def session_token(self):
        return auth.token_from_cookie(self.headers.get("Cookie"))

    def current_user(self):
        with db() as conn:
            user = auth.session_user(conn, self.session_token())
        if not user:
            raise auth.AuthError("Please sign in")
        return user

    def current_workspace(self, q=None):
        # Header from fetch(); ?ws= for plain links (file downloads). The session
        # cookie is SameSite=Strict, so neither works from another site.
        ws_id = self.headers.get("X-Workspace") or (q or {}).get("ws")
        if not ws_id or not ws_id.isdigit():
            raise auth.AuthError("Open a project first", 409)
        with db() as conn:
            return auth.get_workspace(conn, int(ws_id), self.user["email"])

    def is_https(self):
        """True when an HTTPS reverse proxy (Caddy, nginx) forwarded this request."""
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def sign_in(self, email, name=None, picture=None):
        with db() as conn:
            if name is not None:      # Google / dev sign-in: refresh the profile
                auth.upsert_user(conn, email, name, picture or "")
            self.cookie_out = auth.cookie_header(auth.create_session(conn, email), secure=self.is_https())
            return {"user": auth.session_user(conn, auth.token_from_cookie(self.cookie_out.split(";")[0]))}

    def dispatch_auth(self, method, parts, q):
        cfg = auth.load_config(CONFIG_PATH)
        if parts == ["auth", "status"] and method == "GET":
            with db() as conn:
                user = auth.session_user(conn, self.session_token())
                setup = auth.needs_setup(conn)
            return {"needs_setup": setup, "allowed_domains": cfg["allowed_domains"], "user": user,
                    "google_client_id": cfg["google_client_id"] if auth.google_enabled(cfg) else "",
                    "dev_user": DEV_USER}
        if parts == ["auth", "setup"] and method == "POST":
            # First run only: create the admin account; its email domain becomes
            # the domain allowed to register.
            b = self.body()
            with db() as conn:
                if not auth.needs_setup(conn):
                    raise auth.AuthError("Setup is already done -- sign in instead", 403)
                email = auth.clean_email(b.get("email"))
                auth.clean_password(b.get("password"), b.get("confirm"))
                domains = auth.clean_domains(b.get("allowed_domains") or auth.email_domain(email))
                if auth.email_domain(email) not in domains:
                    domains.insert(0, auth.email_domain(email))
                cfg["allowed_domains"] = domains
                auth.save_config(CONFIG_PATH, cfg)
                auth.register(conn, cfg, b.get("name"), email, b["password"], is_admin=True)
            return self.sign_in(email)
        if parts == ["auth", "register"] and method == "POST":
            b = self.body()
            with db() as conn:
                if auth.needs_setup(conn):
                    raise auth.AuthError("Create the first (admin) account first", 409)
                auth.clean_password(b.get("password"), b.get("confirm"))
                email = auth.register(conn, cfg, b.get("name"), b.get("email"), b["password"])
            return self.sign_in(email)
        if parts == ["auth", "login"] and method == "POST":
            b = self.body()
            with db() as conn:
                email = auth.login(conn, b.get("email"), b.get("password"))
            return self.sign_in(email)
        if parts == ["auth", "password"] and method == "POST":
            b = self.body()
            with db() as conn:
                user = auth.session_user(conn, self.session_token())
                if not user:
                    raise auth.AuthError("Please sign in")
                auth.change_password(conn, user["email"], b.get("current"), b.get("new"))
            return {"ok": True}
        if parts == ["auth", "google"] and method == "POST":
            if not auth.google_enabled(cfg):
                raise auth.AuthError("Google sign-in is not enabled", 409)
            email, name, picture = auth.verify_google_token(cfg, self.body().get("credential"))
            return self.sign_in(email, name, picture)
        if parts == ["auth", "dev"] and method == "POST":
            if not DEV_USER:
                raise auth.AuthError("Developer sign-in is disabled", 403)
            return self.sign_in(DEV_USER, DEV_USER.split("@")[0] + " (dev)", "")
        if parts == ["auth", "logout"] and method == "POST":
            with db() as conn:
                auth.end_session(conn, self.session_token())
            self.cookie_out = auth.cookie_header("", clear=True, secure=self.is_https())
            return {"ok": True}
        return None

    # -- workspaces (Jira-style projects)
    def dispatch_workspaces(self, method, rest):
        email = self.user["email"]
        cfg = auth.load_config(CONFIG_PATH)
        if not rest:
            if method == "GET":
                with db() as conn:
                    out = auth.user_workspaces(conn, email)
                    for w in out:
                        w.update(self.workspace_stats(conn, w["id"]))
                return out
            if method == "POST":
                return self.create_workspace(cfg, self.body())
        ws_id = int(rest[0])
        if len(rest) == 1:
            with db() as conn:
                ws = auth.get_workspace(conn, ws_id, email)
                if method == "GET":
                    ws.update(self.workspace_stats(conn, ws_id))
                    ws["connections"] = list_projects(ws_id)
                    return ws
                if method == "PUT":
                    fields = auth.clean_workspace_fields(cfg, self.body(), ws["owner"])
                    if (fields["members"] != json.dumps(ws["members"]) and email != ws["owner"]
                            and not auth.is_admin(conn, email)):
                        raise auth.AuthError("Only the project owner or an admin can change team members", 403)
                    conn.execute(
                        """UPDATE workspaces SET key=?, name=?, description=?, app_url=?, environments=?,
                           members=?, updated_at=? WHERE id=?""",
                        (*fields.values(), now(), ws_id))
                    return auth.get_workspace(conn, ws_id, email)
        return None

    def workspace_stats(self, conn, ws_id):
        by = {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM cases WHERE workspace_id=? GROUP BY status", (ws_id,))}
        total = sum(by.values())
        ran = by.get("Pass", 0) + by.get("Fail", 0)
        last = conn.execute(
            "SELECT id, title, status, started_at, passed, failed, errors, skipped FROM runs "
            "WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (ws_id,)).fetchone()
        return {"cases_total": total, "by_status": by,
                "pass_rate": round(100 * by.get("Pass", 0) / ran) if ran else None,
                "last_run": dict(last) if last else None,
                "automated": conn.execute(
                    """SELECT COUNT(DISTINCT l.case_id) FROM links l JOIN cases c ON c.id = l.case_id
                       WHERE c.workspace_id=?""", (ws_id,)).fetchone()[0]}

    def create_workspace(self, cfg, data):
        """Create a project from the onboarding form; optionally connect its
        automation folder and import every test found as a linked case."""
        email = self.user["email"]
        fields = auth.clean_workspace_fields(cfg, data, email)
        auto = data.get("automation") or {}
        if auto.get("path"):   # validate before creating anything
            if auto.get("framework") not in frameworks.FRAMEWORKS:
                raise ValueError("Pick the automation framework")
            if not Path(auto["path"].strip().strip('"')).is_dir():
                raise ValueError(f"Automation folder not found: {auto['path']}")
        with db() as conn:
            if conn.execute("SELECT 1 FROM workspaces WHERE key=?", (fields["key"],)).fetchone():
                raise ValueError(f"Project key {fields['key']} is already used -- pick another")
            ws_id = conn.execute(
                """INSERT INTO workspaces (key, name, description, app_url, environments, members,
                                           owner, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (*fields.values(), email, now(), now())).lastrowid
        result = {"id": ws_id, "tests": 0, "imported": 0}
        if auto.get("path"):
            conn_id = save_project(None, {"name": auto.get("name") or fields["name"], **auto}, ws_id)
            result["tests"] = conn_id["tests"]
            if auto.get("import_cases", True):
                self.ws = {"id": ws_id}
                result["imported"] = self.generate_cases({"project_id": conn_id["id"]})["created"]
        return result

    # -- project-scoped endpoints (self.ws is the open project)
    def own_case(self, conn, case_id):
        row = conn.execute("SELECT workspace_id FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row or row[0] != self.ws["id"]:
            raise ValueError(f"Test case TC-{case_id:04d} not found in this project")

    def own_node_ids(self, conn, node_ids):
        if not node_ids:
            return []
        marks = ",".join("?" * len(node_ids))
        return [r[0] for r in conn.execute(
            f"""SELECT a.node_id FROM automation_tests a JOIN projects p ON p.id = a.project_id
                WHERE p.workspace_id = ? AND a.node_id IN ({marks})""", [self.ws["id"], *node_ids])]

    def start_run(self, title, node_ids, data):
        return RUNNER.start(self.ws["id"], title, node_ids, bool(data.get("headless")),
                            data.get("region") or (self.ws["environments"] or ["Test"])[0],
                            data.get("extra") or "", self.user["email"])

    def own_run(self, conn, run_id):
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["workspace_id"] != self.ws["id"]:
            return None
        return dict(run)

    def dispatch(self, method, parts, q):
        head = parts[0] if parts else ""
        rest = parts[1:]
        ws_id = self.ws["id"]

        if head == "summary" and method == "GET":
            out = summary(ws_id)
            out["workspace"] = self.ws
            out["user"] = self.user
            return out

        if head == "cases":
            if not rest:
                if method == "GET":
                    return list_cases(q, ws_id)
                if method == "POST":
                    return self.save_case(None, self.body())
            if rest == ["generate"] and method == "POST":
                return self.generate_cases(self.body())
            if rest == ["import", "preview"] and method == "POST":
                b = self.body()
                return importer.preview(b.get("name"), b.get("content"))
            if rest == ["import"] and method == "POST":
                return self.import_cases(self.body())
            if rest == ["run"] and method == "POST":
                return self.run_cases(self.body())
            case_id = int(rest[0])
            with db() as conn:
                self.own_case(conn, case_id)
            if len(rest) == 1:
                if method == "GET":
                    with db() as conn:
                        return case_row(conn, case_id)
                if method == "PUT":
                    return self.save_case(case_id, self.body())
                if method == "DELETE":
                    with db() as conn:
                        conn.execute("DELETE FROM links WHERE case_id=?", (case_id,))
                        conn.execute("DELETE FROM case_history WHERE case_id=?", (case_id,))
                        conn.execute("DELETE FROM test_run_cases WHERE case_id=?", (case_id,))
                        attachments.delete_for_case(conn, case_id)
                        conn.execute("DELETE FROM cases WHERE id=?", (case_id,))
                    return {"ok": True}
            if rest[1:] == ["attachments"] and method == "POST":
                b = self.body()
                out = []
                with db() as conn:
                    tr = int(b["test_run_id"]) if b.get("test_run_id") else None
                    if tr and not conn.execute("SELECT 1 FROM test_runs WHERE id=? AND workspace_id=?",
                                               (tr, ws_id)).fetchone():
                        raise ValueError("Unknown test run")
                    for f in (b.get("files") or [])[:20]:
                        out.append(attachments.add(conn, ATTACH_DIR, ws_id, case_id, f.get("name"), f.get("content"),
                                                   self.user["email"], now(), test_run_id=tr,
                                                   source=f"test run TR-{tr}" if tr else "upload"))
                return out
            if rest[1:] == ["links"] and method == "PUT":
                with db() as conn:
                    node_ids = self.own_node_ids(conn, self.body().get("node_ids", []))
                    conn.execute("DELETE FROM links WHERE case_id=?", (case_id,))
                    conn.executemany("INSERT OR IGNORE INTO links (case_id, node_id) VALUES (?,?)",
                                     [(case_id, n) for n in node_ids])
                    return case_row(conn, case_id)

        if head == "testruns":
            with db() as conn:
                if not rest:
                    if method == "GET":
                        return testruns.list_runs(conn, ws_id)
                    if method == "POST":
                        run_id = testruns.create(conn, ws_id, self.body(), self.user["email"], now())
                        return testruns.get_run(conn, ws_id, run_id)
                run_id = int(rest[0])
                if len(rest) == 1:
                    if method == "GET":
                        run = testruns.get_run(conn, ws_id, run_id)
                        if run:
                            run["attachments"] = attachments.for_test_run(conn, run_id)
                        return run
                    if method == "PUT":
                        return testruns.update_run(conn, ws_id, run_id, self.body(), now())
                    if method == "DELETE":
                        return testruns.delete_run(conn, ws_id, run_id)
                if len(rest) == 3 and rest[1] == "cases" and method == "PUT":
                    return testruns.record(conn, ws_id, run_id, int(rest[2]), self.body(), self.user["email"], now())

        if head == "attachments" and len(rest) == 1:
            with db() as conn:
                if method == "GET":
                    f = attachments.file_for(conn, ws_id, int(rest[0]))
                    if not f:
                        return None
                    body, ctype, name, inline = f
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Disposition", f'{"inline" if inline else "attachment"}; filename="{name}"')
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'; media-src 'self'")
                    self.end_headers()
                    self.wfile.write(body)
                    return SENT
                if method == "DELETE":
                    return attachments.delete(conn, ws_id, int(rest[0]))

        if head == "modules":
            return self.dispatch_modules(method, self.body() if method != "GET" else {})

        if head == "reports" and method == "GET":
            with db() as conn:
                return testruns.reports(conn, ws_id, q.get("scope", ""))

        if head == "results" and rest == ["upload"] and method == "POST":
            return self.upload_results(self.body())

        if head == "automation":
            if not rest and method == "GET":
                return list_automation(q, ws_id)
            if rest == ["sync"] and method == "POST":
                total = 0
                for p in list_projects(ws_id):
                    total += scan_project(p["id"])
                return {"count": total}

        if head == "projects":    # automation connections of this project
            if not rest:
                if method == "GET":
                    return list_projects(ws_id)
                if method == "POST":
                    return save_project(None, self.body(), ws_id)
            project_id = int(rest[0])
            if len(rest) == 1:
                if method == "PUT":
                    return save_project(project_id, self.body(), ws_id)
                if method == "DELETE":
                    return delete_project(project_id, ws_id)
            if rest[1:] == ["scan"] and method == "POST":
                with db() as conn:
                    owned_project(conn, project_id, ws_id)
                return {"tests": scan_project(project_id)}

        if head == "runs":
            if not rest:
                if method == "GET":
                    with db() as conn:
                        return rows(conn.execute(
                            "SELECT * FROM runs WHERE workspace_id=? ORDER BY id DESC LIMIT 100", (ws_id,)))
                if method == "POST":
                    b = self.body()
                    if not b.get("node_ids"):
                        raise ValueError("Nothing to run")
                    return {"run_id": self.start_run(b.get("title") or "Ad-hoc run", b["node_ids"], b)}
            if rest == ["suite"] and method == "POST":
                return self.run_suite(self.body())
            run_id = int(rest[0])
            with db() as conn:
                run = self.own_run(conn, run_id)
                if not run:
                    return None
                if len(rest) == 1 and method == "GET":
                    run["results"] = rows(conn.execute(
                        """SELECT r.*, a.line, a.file, p.path AS root, p.name AS project,
                                  (SELECT GROUP_CONCAT(l.case_id) FROM links l WHERE l.node_id = r.node_id) AS case_ids
                           FROM run_results r LEFT JOIN automation_tests a ON a.node_id = r.node_id
                           LEFT JOIN projects p ON p.id = a.project_id
                           WHERE r.run_id=? ORDER BY r.rowid""", (run_id,)))
                    run["attachments"] = attachments.for_run(conn, run_id)
                    return run
            if rest[1:] == ["report"] and method == "GET":
                self.send_html(build_report(run_id), download=q.get("download") == "1",
                               name=f"run_{run_id}_report.html")
                return SENT
            if rest[1:] == ["log"] and method == "GET":
                offset = int(q.get("offset", 0))
                text = ""
                if run["log_path"] and Path(run["log_path"]).exists():
                    with open(run["log_path"], "rb") as fh:
                        fh.seek(offset)
                        chunk = fh.read(256_000)
                    offset += len(chunk)
                    text = chunk.decode("utf-8", errors="replace")
                return {"text": text, "offset": offset, "status": run["status"]}
            if rest[1:] == ["cancel"] and method == "POST":
                return {"ok": RUNNER.cancel(run_id)}
        return None

    def save_case(self, case_id, data):
        values = {f: (data.get(f) or "").strip() if isinstance(data.get(f), str) else data.get(f)
                  for f in CASE_FIELDS if f in data}
        if case_id is None and not values.get("title"):
            raise ValueError("Title is required")
        if values.get("title"):
            values["title"] = format_title(values["title"])
        for f in ("module", "section"):
            if values.get(f):
                values[f] = format_name(values[f])
        if values.get("status") and values["status"] not in CASE_STATUSES:
            raise ValueError(f"Unknown status {values['status']}")
        stamp = now()
        with db() as conn:
            if case_id is None:
                values.setdefault("status", "Not Run")
                values.setdefault("priority", "Medium")
                values["created_at"] = values["updated_at"] = stamp
                values["workspace_id"] = self.ws["id"]
                cols = ", ".join(values)
                cur = conn.execute(f"INSERT INTO cases ({cols}) VALUES ({', '.join('?' * len(values))})",
                                   list(values.values()))
                case_id = cur.lastrowid
            else:
                old = conn.execute("SELECT status FROM cases WHERE id=?", (case_id,)).fetchone()
                if not old:
                    return None
                values["updated_at"] = stamp
                conn.execute(f"UPDATE cases SET {', '.join(f'{k}=?' for k in values)} WHERE id=?",
                             [*values.values(), case_id])
                if "status" in values and values["status"] != old["status"]:
                    conn.execute("INSERT INTO case_history (case_id, run_id, status, at, source) VALUES (?,?,?,?,?)",
                                 (case_id, None, values["status"], stamp, "manual: " + self.user["email"]))
            if "node_ids" in data:
                node_ids = self.own_node_ids(conn, data["node_ids"])
                conn.execute("DELETE FROM links WHERE case_id=?", (case_id,))
                conn.executemany("INSERT OR IGNORE INTO links (case_id, node_id) VALUES (?,?)",
                                 [(case_id, n) for n in node_ids])
            return case_row(conn, case_id)

    def import_cases(self, data):
        """Create (or update) cases from an Excel / CSV sheet confirmed in the import dialog."""
        recs = importer.cases_from_upload(data.get("name"), data.get("content"), data.get("sheets") or [])
        if not recs:
            raise ValueError("No test cases found -- map a column to Title (and check the header row)")
        update = data.get("duplicates") == "update"
        ws_id, stamp, who = self.ws["id"], now(), self.user["email"]
        created = updated = skipped = 0
        with db() as conn:
            for rec in recs:
                rec = {k: v for k, v in rec.items() if k in CASE_FIELDS and v not in (None, "")}
                if rec.get("title"):
                    rec["title"] = format_title(rec["title"])
                for f in ("module", "section"):
                    if rec.get(f):
                        rec[f] = format_name(rec[f])
                existing = None
                if rec.get("ref"):
                    existing = conn.execute("SELECT * FROM cases WHERE workspace_id=? AND ref=? AND module=?",
                                            (ws_id, rec["ref"], rec.get("module", ""))).fetchone()
                    m = re.fullmatch(r"TC-(\d{4,})", rec["ref"].strip(), re.I)     # our own export's IDs
                    if not existing and m:
                        existing = conn.execute(
                            "SELECT * FROM cases WHERE workspace_id=? AND id=? AND (title=? OR module=?)",
                            (ws_id, int(m.group(1)), rec["title"], rec.get("module", ""))).fetchone()
                        if existing:
                            rec.pop("ref")
                if not existing:
                    existing = conn.execute("SELECT * FROM cases WHERE workspace_id=? AND title=? AND module=?",
                                            (ws_id, rec["title"], rec.get("module", ""))).fetchone()
                conn.execute("INSERT OR IGNORE INTO modules (workspace_id, module, section, created_at) VALUES (?,?,?,?)",
                             (ws_id, rec.get("module", ""), rec.get("section", ""), stamp))
                if existing:
                    if not update:
                        skipped += 1
                        continue
                    conn.execute(f"UPDATE cases SET {', '.join(f'{k}=?' for k in rec)}, updated_at=? WHERE id=?",
                                 [*rec.values(), stamp, existing["id"]])
                    if rec.get("status") and rec["status"] != existing["status"]:
                        conn.execute("INSERT INTO case_history (case_id, run_id, status, at, source) VALUES (?,?,?,?,?)",
                                     (existing["id"], None, rec["status"], stamp, f"import: {who}"))
                    updated += 1
                    continue
                rec.setdefault("priority", "Medium")
                rec.setdefault("status", "Not Run")
                rec.update(workspace_id=ws_id, created_at=stamp, updated_at=stamp)
                cid = conn.execute(f"INSERT INTO cases ({', '.join(rec)}) VALUES ({', '.join('?' * len(rec))})",
                                   list(rec.values())).lastrowid
                if rec["status"] != "Not Run":
                    conn.execute("INSERT INTO case_history (case_id, run_id, status, at, source) VALUES (?,?,?,?,?)",
                                 (cid, None, rec["status"], stamp, f"import: {who}"))
                created += 1
        return {"created": created, "updated": updated, "skipped": skipped, "total": len(recs)}

    def dispatch_modules(self, method, b):
        """Modules (suites) and their sections: list, add, rename, delete / move."""
        ws_id, stamp = self.ws["id"], now()
        clean = lambda v: re.sub(r"\s+", " ", str(v or "")).strip()[:120]
        module, section, name = clean(b.get("module")), clean(b.get("section")), format_name(clean(b.get("name")))
        with db() as conn:
            if method == "GET":
                return summary(ws_id)["sections"]
            if method == "POST":
                if not name:
                    raise ValueError("Enter a name")
                if module:      # a new section in an existing module
                    conn.execute("INSERT OR IGNORE INTO modules (workspace_id, module, section, created_at) VALUES (?,?,?,?)",
                                 (ws_id, module, "", stamp))
                    conn.execute("INSERT OR IGNORE INTO modules (workspace_id, module, section, created_at) VALUES (?,?,?,?)",
                                 (ws_id, module, name, stamp))
                else:
                    conn.execute("INSERT OR IGNORE INTO modules (workspace_id, module, section, created_at) VALUES (?,?,?,?)",
                                 (ws_id, name, "", stamp))
                return {"ok": True}
            if method == "PUT":            # rename
                if not name:
                    raise ValueError("Enter the new name")
                if "section" in b:
                    conn.execute("UPDATE cases SET section=?, updated_at=? WHERE workspace_id=? AND module=? AND section=?",
                                 (name, stamp, ws_id, module, section))
                    conn.execute("UPDATE OR IGNORE modules SET section=? WHERE workspace_id=? AND module=? AND section=?",
                                 (name, ws_id, module, section))
                    conn.execute("DELETE FROM modules WHERE workspace_id=? AND module=? AND section=?", (ws_id, module, section))
                    conn.execute("INSERT OR IGNORE INTO modules (workspace_id, module, section, created_at) VALUES (?,?,?,?)",
                                 (ws_id, module, name, stamp))
                else:
                    conn.execute("UPDATE cases SET module=?, updated_at=? WHERE workspace_id=? AND module=?",
                                 (name, stamp, ws_id, module))
                    for r in conn.execute("SELECT section FROM modules WHERE workspace_id=? AND module=?",
                                          (ws_id, module)).fetchall():
                        conn.execute("INSERT OR IGNORE INTO modules (workspace_id, module, section, created_at) VALUES (?,?,?,?)",
                                     (ws_id, name, r["section"], stamp))
                    conn.execute("DELETE FROM modules WHERE workspace_id=? AND module=?", (ws_id, module))
                    conn.execute("INSERT OR IGNORE INTO modules (workspace_id, module, section, created_at) VALUES (?,?,?,?)",
                                 (ws_id, name, "", stamp))
                return {"ok": True}
            if method == "DELETE":
                is_section = "section" in b
                where, args = ("module=? AND section=?", [module, section]) if is_section else ("module=?", [module])
                n = conn.execute(f"SELECT COUNT(*) FROM cases WHERE workspace_id=? AND {where}", [ws_id, *args]).fetchone()[0]
                move_to = b.get("move_to")
                if n and move_to is None:
                    raise ValueError(f"{n} test case(s) are in it -- choose where to move them first")
                if n:
                    target = clean(move_to)
                    if is_section:
                        conn.execute(f"UPDATE cases SET section=?, updated_at=? WHERE workspace_id=? AND {where}",
                                     [target, stamp, ws_id, *args])
                    else:
                        conn.execute(f"UPDATE cases SET module=?, updated_at=? WHERE workspace_id=? AND {where}",
                                     [target, stamp, ws_id, *args])
                conn.execute(f"DELETE FROM modules WHERE workspace_id=? AND {where}", [ws_id, *args])
                return {"ok": True, "moved": n}
        return None

    def upload_results(self, data):
        """Results file(s) from a run made elsewhere (CI, a teammate, the IDE):
        JUnit XML from pytest, Playwright, Selenium/Maven Surefire, TestNG...
        Stored as an 'Imported' run; linked cases update as for a run started here."""
        with db() as conn:
            project = owned_project(conn, int(data.get("project_id") or 0), self.ws["id"])
        files = data.get("files") or []
        if not files:
            raise ValueError("Choose at least one results file (JUnit XML)")
        if RUNNER.busy():
            raise RuntimeError(f"Run #{RUNNER.run_id} is in progress -- upload when it finishes.")
        for f in files[:50]:
            text = f.get("content") or ""
            if "<testcase" not in text and "<testsuite" not in text:
                raise ValueError(f"{f.get('name') or 'file'} is not a JUnit XML results file")
        with db() as conn:
            run_id = conn.execute(
                """INSERT INTO runs (workspace_id, title, targets, options, status, started_at, finished_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (self.ws["id"], (data.get("title") or "").strip() or f"Uploaded results -- {project['name']}", "[]",
                 json.dumps({"uploaded_by": self.user["email"], "started_by": self.user["email"]}),
                 "Imported", now(), now())).lastrowid
        paths = []
        for i, f in enumerate(files[:50]):
            path = RUNS_DIR / f"run_{run_id}_upload_{i}.xml"
            path.write_text(f.get("content") or "", encoding="utf-8")
            paths.append(path)
        try:
            updated = apply_results(run_id, paths, project["id"], source=f"upload: {self.user['email']}")
        except ET.ParseError as exc:
            with db() as conn:
                conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
            raise ValueError(f"Unreadable XML: {exc}")
        with db() as conn:
            run = dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if not (run["passed"] + run["failed"] + run["errors"] + run["skipped"]):
                conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
                raise ValueError("None of the results match a test in this connection -- pick the right "
                                 "connection, or re-scan it first")
            conn.execute("UPDATE runs SET status=? WHERE id=?",
                         ("Passed" if not (run["failed"] + run["errors"]) else "Failed", run_id))
        write_report(run_id)
        return {"run_id": run_id, "updated_cases": updated, "passed": run["passed"],
                "failed": run["failed"] + run["errors"], "skipped": run["skipped"]}

    def generate_cases(self, data):
        """One case per automation test of this project that has no linked case yet."""
        sql = """SELECT a.* FROM automation_tests a JOIN projects p ON p.id = a.project_id
                 WHERE a.present = 1 AND p.workspace_id = ?
                 AND NOT EXISTS (SELECT 1 FROM links l WHERE l.node_id = a.node_id)"""
        args = [self.ws["id"]]
        if data.get("suite"):
            sql += " AND a.suite = ?"
            args.append(data["suite"])
        if data.get("project_id"):
            sql += " AND a.project_id = ?"
            args.append(int(data["project_id"]))
        if data.get("node_ids"):
            sql += f" AND a.node_id IN ({','.join('?' * len(data['node_ids']))})"
            args += data["node_ids"]
        stamp = now()
        created = 0
        with db() as conn:
            for t in conn.execute(sql + " ORDER BY a.project_id, a.file, a.line", args).fetchall():
                status = {"passed": "Pass", "failed": "Fail", "error": "Fail",
                          "skipped": "Skipped"}.get(t["last_result"], "Not Run")
                cur = conn.execute(
                    """INSERT INTO cases (workspace_id, title, module, section, priority, steps, status, last_run_id,
                                          last_run_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (self.ws["id"], humanize(t["name"]), t["suite"], section_from_cls(t["cls"]), "Medium", t["doc"], status,
                     t["last_run_id"], t["last_run_at"], stamp, stamp))
                conn.execute("INSERT INTO links (case_id, node_id) VALUES (?,?)", (cur.lastrowid, t["node_id"]))
                created += 1
        return {"created": created}

    def run_suite(self, data):
        """Run every test of a connection, of one suite in it, or of the whole project."""
        sql = """SELECT a.node_id FROM automation_tests a JOIN projects p ON p.id = a.project_id
                 WHERE a.present = 1 AND p.workspace_id = ?"""
        args, parts = [self.ws["id"]], []
        if data.get("project_id"):
            sql += " AND a.project_id = ?"
            args.append(int(data["project_id"]))
        if data.get("suite"):
            sql += " AND a.suite = ?"
            args.append(data["suite"])
        with db() as conn:
            node_ids = [r[0] for r in conn.execute(sql + " ORDER BY a.project_id, a.file, a.line", args)]
            if data.get("project_id"):
                parts.append(owned_project(conn, int(data["project_id"]), self.ws["id"])["name"])
        if not node_ids:
            raise ValueError("No automation tests found for that selection -- scan the project first")
        parts.append(data.get("suite") or "all suites")
        title = data.get("title") or ("Run all -- " + " / ".join(parts) if data.get("project_id")
                                      else f"Run all -- {self.ws['name']}")
        return {"run_id": self.start_run(title, node_ids, data), "tests": len(node_ids)}

    def run_cases(self, data):
        """Run the automation linked to the given cases, or to every case in a module."""
        case_ids = data.get("case_ids") or []
        with db() as conn:
            if "module" in data:
                case_ids = [r[0] for r in conn.execute(
                    "SELECT id FROM cases WHERE module = ? AND workspace_id = ?",
                    (data["module"] or "", self.ws["id"]))]
                if not case_ids:
                    raise ValueError(f"Module '{data['module']}' has no test cases")
                data.setdefault("title", f"Module -- {data['module'] or '(no module)'}")
            if not case_ids:
                raise ValueError("Select at least one test case")
            node_ids = [r[0] for r in conn.execute(
                f"""SELECT DISTINCT l.node_id FROM links l JOIN automation_tests a ON a.node_id = l.node_id
                    JOIN cases c ON c.id = l.case_id
                    WHERE a.present = 1 AND c.workspace_id = ? AND l.case_id IN ({','.join('?' * len(case_ids))})
                    ORDER BY a.project_id, a.file, a.line""", [self.ws["id"], *case_ids])]
        if not node_ids:
            raise ValueError("None of the selected cases is linked to an automation test")
        title = data.get("title") or f"{len(case_ids)} test case(s)"
        return {"run_id": self.start_run(title, node_ids, data), "tests": len(node_ids)}


class SingleInstanceServer(ThreadingHTTPServer):
    """Refuse to start when the port is taken.

    HTTPServer sets SO_REUSEADDR, which on Windows lets a second server bind the
    same port silently -- two copies then share requests and runs. Exclusive
    binding makes the second copy fail fast instead.
    """
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            import socket
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    parser = argparse.ArgumentParser(description="CFlow Test Case Manager")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on: 127.0.0.1 = this computer only (default), "
                             "0.0.0.0 = the network (also list the address in data/config.json allowed_hosts)")
    parser.add_argument("--import-junit", metavar="XML",
                        help="record an external run's junit xml, update linked cases, and exit")
    parser.add_argument("--project", type=int, default=FIRST_PROJECT_ID,
                        help="automation connection id the --import-junit results belong to (default: 1)")
    parser.add_argument("--dev-user", metavar="EMAIL",
                        help="LOCAL TESTING ONLY: allow signing in as EMAIL without Google")
    args = parser.parse_args()

    global DEV_USER, ALLOWED_HOSTS
    ALLOWED_HOSTS = {h.strip().lower() for h in auth.load_config(CONFIG_PATH)["allowed_hosts"] if h.strip()}
    hosted = args.host not in LOCAL_HOSTS
    if hosted and args.dev_user:
        print("[ERROR] --dev-user lets anyone sign in without a password; it cannot be used with --host.")
        return 1
    if hosted:
        if not ALLOWED_HOSTS:
            print('[WARN] --host is set but data/config.json has no "allowed_hosts" -- other computers '
                  'will get "forbidden". Add the address people will open, e.g. "allowed_hosts": ["192.168.1.20"].')
        print(f"[WARN] Listening on {args.host}: anyone who can reach this address can see the sign-in "
              "page, and signed-in users can run test commands on this machine.")
    if args.dev_user:
        DEV_USER = args.dev_user.strip().lower()
        print(f"[WARN] Developer sign-in enabled for {DEV_USER} -- anyone who can open this page "
              "can sign in as that user. Do not use this for normal work.")

    init_db()
    print(f"[INFO] {sync_automation()} automation tests discovered across connected projects")

    if args.import_junit:
        with db() as conn:
            ws_id = get_project(conn, args.project)["workspace_id"]
            run_id = conn.execute(
                """INSERT INTO runs (workspace_id, title, targets, status, started_at, finished_at, junit_path)
                   VALUES (?,?,?,?,?,?,?)""",
                (ws_id, f"Imported {Path(args.import_junit).name}", "[]", "Imported", now(), now(),
                 str(Path(args.import_junit).resolve()))).lastrowid
        updated = apply_results(run_id, [args.import_junit], args.project, source="import")
        print(f"[OK] run #{run_id} imported; {updated} linked case(s) updated")
        return

    try:
        server = SingleInstanceServer((args.host, args.port), Handler)
    except OSError:
        print(f"[ERROR] Port {args.port} is already in use -- the Test Case Manager is probably already "
              f"running (open http://localhost:{args.port}), or use --port.")
        return 1
    print(f"[OK] Test Case Manager running at http://localhost:{args.port}  (Ctrl+C to stop)")
    for h in sorted(ALLOWED_HOSTS) if hosted else ():
        print(f"     also open at http://{h}:{args.port} (or your HTTPS address if behind a proxy)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
