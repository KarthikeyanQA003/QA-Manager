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
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))  # so the sibling modules import from any working directory
import auth  # noqa: E402
import codegen  # noqa: E402
import frameworks  # noqa: E402

DATA_DIR = TOOL_DIR / "data"
RUNS_DIR = DATA_DIR / "runs"
DB_PATH = DATA_DIR / "tcm.db"
CONFIG_PATH = DATA_DIR / "config.json"

# Set by --dev-user: enables a sign-in without Google for that one address,
# for local testing only. Off unless the server is started with the flag.
DEV_USER = None

CASE_STATUSES = ["Not Run", "Pass", "Fail", "Blocked", "Skipped"]
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
    run_id INTEGER, node_id TEXT, outcome TEXT, duration REAL, message TEXT
);
CREATE TABLE IF NOT EXISTS case_history (
    case_id INTEGER, run_id INTEGER, status TEXT, at TEXT, source TEXT
);
"""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def humanize(test_name):
    """test_TC_WFS_003_form_creation_main_section -> 'TC_WFS_003 Form creation main section'."""
    if " " in test_name:          # already a sentence (Playwright / JUnit5 display names)
        return test_name
    name = re.sub(r"^test_?", "", test_name)
    m = re.match(r"((?:TC|tc)[_A-Za-z]*?_?\d+[A-Za-z]?)_(.*)", name)
    if m:
        rest = m.group(2).replace("_", " ").strip()
        return f"{m.group(1)} {rest[:1].upper()}{rest[1:]}"
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").strip()   # camelCase too
    return name[:1].upper() + name[1:]


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
            conn.execute(
                "INSERT INTO run_results (run_id, node_id, outcome, duration, message) VALUES (?,?,?,?,?)",
                (run_id, node_id, outcome, duration, message),
            )
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
                status = case_status_from(outcomes)
                conn.execute(
                    "UPDATE cases SET status=?, last_run_id=?, last_run_at=?, updated_at=? WHERE id=?",
                    (status, run_id, stamp, stamp, case_id),
                )
                conn.execute(
                    "INSERT INTO case_history (case_id, run_id, status, at, source) VALUES (?,?,?,?,?)",
                    (case_id, run_id, status, stamp, source),
                )
                updated += 1

        # A run can span several projects; accumulate.
        conn.execute(
            """UPDATE runs SET passed=passed+?, failed=failed+?, skipped=skipped+?, errors=errors+?
               WHERE id=?""",
            (counts["passed"], counts["failed"], counts["skipped"], counts["error"], run_id),
        )
    return updated


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

            groups = {}
            for t in tests:
                groups.setdefault(t["project_id"], []).append(t)
            self.active, self.run_id = True, run_id
            threading.Thread(target=self._run_all, args=(run_id, groups, options, log_path),
                             daemon=True).start()
            return run_id

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

        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", FORCE_COLOR="0",
                   **frameworks.run_env(framework, junit))
        # CREATE_NO_WINDOW: never pop a console window for the run (output goes
        # to the run log). GUI windows -- the browser in headed mode -- still show.
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
        started = time.time()
        # shell=True: npx / mvn are .cmd scripts on Windows, and the command is a
        # user-editable template.
        self.proc = subprocess.Popen(cmd, shell=True, cwd=p["path"], stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=flags, env=env)
        code = self.proc.wait()
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


RUNNER = Runner()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

CASE_FIELDS = ["title", "module", "priority", "preconditions", "steps", "expected", "notes", "status"]


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
    return case


def list_cases(q, ws_id):
    sql = """SELECT c.*, (SELECT COUNT(*) FROM links l WHERE l.case_id = c.id) AS link_count
             FROM cases c WHERE c.workspace_id = ?"""
    args = [ws_id]
    if q.get("q"):
        sql += " AND (c.title LIKE ? OR c.steps LIKE ? OR c.expected LIKE ? OR ('TC-' || printf('%04d', c.id)) LIKE ?)"
        like = f"%{q['q']}%"
        args += [like] * 4
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
                      SUM(status IN ('Blocked','Skipped')) blocked, SUM(status='Not Run') notrun
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
        suites = [r[0] for r in conn.execute(
            """SELECT DISTINCT a.suite FROM automation_tests a JOIN projects p ON p.id = a.project_id
               WHERE a.present = 1 AND p.workspace_id = ? ORDER BY a.suite""", (ws_id,))]
    return {
        "by_status": by_status, "modules": modules, "cases_total": cases_total,
        "cases_linked": cases_linked, "auto_total": auto["total"], "auto_linked": auto["linked"] or 0,
        "recent_runs": runs, "suites": suites, "statuses": CASE_STATUSES, "priorities": PRIORITIES,
        "running": RUNNER.run_id if RUNNER.busy() else None,
        "frameworks": {k: v["label"] for k, v in frameworks.FRAMEWORKS.items()},
        "framework_commands": {k: v["command"] for k, v in frameworks.FRAMEWORKS.items()},
        "projects": [{"id": p["id"], "name": p["name"], "framework": p["framework"]} for p in list_projects(ws_id)],
    }


EXPORT_COLUMNS = ["ID", "Title", "Module", "Priority", "Status", "Preconditions", "Steps",
                  "Expected Result", "Notes", "Automation Tests", "Last Run", "Created", "Updated"]


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
    return [[f"TC-{c['id']:04d}", c["title"], c["module"], c["priority"], c["status"],
             c["preconditions"], c["steps"], c["expected"], c["notes"],
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
    widths = [10, 50, 26, 10, 10, 30, 60, 40, 30, 70, 19, 19, 19]
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
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("localhost", "127.0.0.1", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            o = urlparse(origin)
            if o.hostname not in ("localhost", "127.0.0.1", "::1") or o.port != self.server.server_address[1]:
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

    def sign_in(self, email, name=None, picture=None):
        with db() as conn:
            if name is not None:      # Google / dev sign-in: refresh the profile
                auth.upsert_user(conn, email, name, picture or "")
            self.cookie_out = auth.cookie_header(auth.create_session(conn, email))
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
            self.cookie_out = auth.cookie_header("", clear=True)
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
                        conn.execute("DELETE FROM cases WHERE id=?", (case_id,))
                    return {"ok": True}
            if rest[1:] == ["links"] and method == "PUT":
                with db() as conn:
                    node_ids = self.own_node_ids(conn, self.body().get("node_ids", []))
                    conn.execute("DELETE FROM links WHERE case_id=?", (case_id,))
                    conn.executemany("INSERT OR IGNORE INTO links (case_id, node_id) VALUES (?,?)",
                                     [(case_id, n) for n in node_ids])
                    return case_row(conn, case_id)

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
                    return run
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
                    """INSERT INTO cases (workspace_id, title, module, priority, steps, status, last_run_id,
                                          last_run_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (self.ws["id"], humanize(t["name"]), t["suite"], "Medium", t["doc"], status,
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
    parser.add_argument("--import-junit", metavar="XML",
                        help="record an external run's junit xml, update linked cases, and exit")
    parser.add_argument("--project", type=int, default=FIRST_PROJECT_ID,
                        help="automation connection id the --import-junit results belong to (default: 1)")
    parser.add_argument("--dev-user", metavar="EMAIL",
                        help="LOCAL TESTING ONLY: allow signing in as EMAIL without Google")
    args = parser.parse_args()

    global DEV_USER
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
        server = SingleInstanceServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(f"[ERROR] Port {args.port} is already in use -- the Test Case Manager is probably already "
              f"running (open http://localhost:{args.port}), or use --port.")
        return 1
    print(f"[OK] Test Case Manager running at http://localhost:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
