"""
Manual test execution and release reporting.

A test run is a chosen set of test cases (by module, section, priority or by
hand) executed by a tester one case at a time. Each case gets a result --
Passed, Failed, Blocked, Retest or Skipped -- with the actual result, a comment
and a defect id. Recording a result also sets the test case's status (Board,
Dashboard) and adds a line to its history.

reports() turns the project's cases -- or one test run -- into pass rate,
progress, coverage, failures, trend and a release-readiness verdict.
"""

RESULTS = ["Untested", "Passed", "Failed", "Blocked", "Retest", "Skipped"]
RESULT_TO_STATUS = {"Passed": "Pass", "Failed": "Fail", "Blocked": "Blocked", "Retest": "Retest",
                    "Skipped": "Skipped", "Untested": "Not Run"}
STATUS_TO_RESULT = {v: k for k, v in RESULT_TO_STATUS.items()}

# Release readiness: every gate must hold for "Ready".
READY_EXECUTED_PCT = 95
READY_PASS_PCT = 95
AT_RISK_PCT = 85

SCHEMA = """
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    release TEXT DEFAULT '',
    environment TEXT DEFAULT '',
    status TEXT DEFAULT 'Open',
    created_by TEXT, created_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS test_run_cases (
    test_run_id INTEGER, case_id INTEGER,
    result TEXT DEFAULT 'Untested',
    actual TEXT DEFAULT '', comment TEXT DEFAULT '', defect TEXT DEFAULT '',
    executed_by TEXT, executed_at TEXT,
    PRIMARY KEY (test_run_id, case_id)
);
"""


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def select_cases(conn, ws_id, data):
    """Case ids for a new run: explicit ids, or every case matching the chosen
    modules / sections / priorities (all cases when nothing is chosen)."""
    if data.get("case_ids"):
        ids = [int(i) for i in data["case_ids"]]
        marks = ",".join("?" * len(ids))
        return [r[0] for r in conn.execute(
            f"SELECT id FROM cases WHERE workspace_id=? AND id IN ({marks}) ORDER BY module, section, id",
            [ws_id, *ids])]
    sql, args = "SELECT id FROM cases WHERE workspace_id=?", [ws_id]
    for field, key in (("module", "modules"), ("section", "sections"), ("priority", "priorities")):
        values = [v for v in data.get(key) or [] if isinstance(v, str)]
        if values:
            sql += f" AND {field} IN ({','.join('?' * len(values))})"
            args += values
    if data.get("only") == "manual":
        sql += " AND NOT EXISTS (SELECT 1 FROM links l WHERE l.case_id = cases.id)"
    elif data.get("only") == "automated":
        sql += " AND EXISTS (SELECT 1 FROM links l WHERE l.case_id = cases.id)"
    return [r[0] for r in conn.execute(sql + " ORDER BY module, section, id", args)]


def create(conn, ws_id, data, user, stamp):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Give the test run a name")
    case_ids = select_cases(conn, ws_id, data)
    if not case_ids:
        raise ValueError("No test cases match -- choose other modules or sections")
    run_id = conn.execute(
        """INSERT INTO test_runs (workspace_id, name, description, release, environment, status, created_by, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (ws_id, name, (data.get("description") or "").strip(), (data.get("release") or "").strip(),
         (data.get("environment") or "").strip(), "Open", user, stamp)).lastrowid
    conn.executemany("INSERT OR IGNORE INTO test_run_cases (test_run_id, case_id) VALUES (?,?)",
                     [(run_id, c) for c in case_ids])
    return run_id


def counts_sql(alias="trc"):
    return ", ".join(f"SUM({alias}.result = '{r}') AS \"{r.lower()}\"" for r in RESULTS)


def list_runs(conn, ws_id):
    return _rows(conn.execute(
        f"""SELECT t.*, COUNT(trc.case_id) AS total, {counts_sql()}
            FROM test_runs t LEFT JOIN test_run_cases trc ON trc.test_run_id = t.id
            WHERE t.workspace_id = ? GROUP BY t.id ORDER BY t.id DESC""", (ws_id,)))


def get_run(conn, ws_id, run_id):
    run = conn.execute("SELECT * FROM test_runs WHERE id=? AND workspace_id=?", (run_id, ws_id)).fetchone()
    if not run:
        return None
    run = dict(run)
    run["cases"] = _rows(conn.execute(
        """SELECT trc.*, c.title, c.module, c.section, c.priority, c.preconditions, c.steps, c.expected,
                  c.test_data, c.notes, c.status AS case_status,
                  (SELECT COUNT(*) FROM links l WHERE l.case_id = c.id) AS link_count
           FROM test_run_cases trc JOIN cases c ON c.id = trc.case_id
           WHERE trc.test_run_id = ? ORDER BY c.module, c.section, c.id""", (run_id,)))
    return run


def update_run(conn, ws_id, run_id, data, stamp):
    run = conn.execute("SELECT * FROM test_runs WHERE id=? AND workspace_id=?", (run_id, ws_id)).fetchone()
    if not run:
        return None
    fields = {k: (data[k] or "").strip() for k in ("name", "description", "release", "environment")
              if isinstance(data.get(k), str)}
    if fields.get("name") == "":
        raise ValueError("Give the test run a name")
    if data.get("status") in ("Open", "Closed"):
        fields["status"] = data["status"]
        fields["closed_at"] = stamp if data["status"] == "Closed" else None
    if fields:
        conn.execute(f"UPDATE test_runs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                     [*fields.values(), run_id])
    if data.get("add_case_ids"):
        if run["status"] == "Closed":
            raise ValueError("This test run is closed -- reopen it to add cases")
        ids = select_cases(conn, ws_id, {"case_ids": data["add_case_ids"]})
        conn.executemany("INSERT OR IGNORE INTO test_run_cases (test_run_id, case_id) VALUES (?,?)",
                         [(run_id, c) for c in ids])
    return get_run(conn, ws_id, run_id)


def record(conn, ws_id, run_id, case_id, data, user, stamp):
    """Record one case's result in a run and carry it onto the case."""
    run = conn.execute("SELECT * FROM test_runs WHERE id=? AND workspace_id=?", (run_id, ws_id)).fetchone()
    if not run:
        return None
    if run["status"] == "Closed":
        raise ValueError("This test run is closed -- reopen it to record results")
    result = data.get("result") or "Untested"
    if result not in RESULTS:
        raise ValueError(f"Unknown result {result}")
    row = conn.execute("SELECT * FROM test_run_cases WHERE test_run_id=? AND case_id=?", (run_id, case_id)).fetchone()
    if not row:
        raise ValueError("That case is not in this test run")
    text = {k: (data.get(k) if isinstance(data.get(k), str) else row[k]) or "" for k in ("actual", "comment", "defect")}
    conn.execute(
        """UPDATE test_run_cases SET result=?, actual=?, comment=?, defect=?, executed_by=?, executed_at=?
           WHERE test_run_id=? AND case_id=?""",
        (result, text["actual"].strip(), text["comment"].strip(), text["defect"].strip(),
         user if result != "Untested" else None, stamp if result != "Untested" else None, run_id, case_id))
    if result != row["result"]:
        status = RESULT_TO_STATUS[result]
        conn.execute("UPDATE cases SET status=?, updated_at=? WHERE id=?", (status, stamp, case_id))
        note = f" · defect {text['defect'].strip()}" if text["defect"].strip() else ""
        conn.execute("INSERT INTO case_history (case_id, run_id, status, at, source) VALUES (?,?,?,?,?)",
                     (case_id, None, status, stamp, f"test run TR-{run_id}: {user}{note}"))
    return dict(conn.execute("SELECT * FROM test_run_cases WHERE test_run_id=? AND case_id=?",
                             (run_id, case_id)).fetchone())


def delete_run(conn, ws_id, run_id):
    if not conn.execute("SELECT 1 FROM test_runs WHERE id=? AND workspace_id=?", (run_id, ws_id)).fetchone():
        return None
    conn.execute("DELETE FROM test_run_cases WHERE test_run_id=?", (run_id,))
    conn.execute("DELETE FROM test_runs WHERE id=?", (run_id,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Reports and release readiness
# ---------------------------------------------------------------------------

def _pct(n, d):
    return round(100 * n / d, 1) if d else 0.0


def _summarise(items):
    """items: dicts with 'status' (case status vocabulary)."""
    c = {s: 0 for s in ("Pass", "Fail", "Blocked", "Retest", "Skipped", "Not Run", "Running")}
    for it in items:
        c[it["status"] if it["status"] in c else "Not Run"] += 1
    total = len(items)
    executed = c["Pass"] + c["Fail"] + c["Blocked"] + c["Retest"] + c["Skipped"]
    judged = c["Pass"] + c["Fail"] + c["Blocked"] + c["Retest"]
    return {"total": total, "counts": c, "executed": executed, "executed_pct": _pct(executed, total),
            "pass_rate": _pct(c["Pass"], judged)}


def reports(conn, ws_id, scope=""):
    """Everything the Reports page shows, for the project or one test run
    (scope 'run:<id>')."""
    run = None
    if scope.startswith("run:"):
        run = conn.execute("SELECT * FROM test_runs WHERE id=? AND workspace_id=?",
                           (int(scope[4:]), ws_id)).fetchone()
        if not run:
            raise ValueError("Unknown test run")
        items = _rows(conn.execute(
            """SELECT c.id, c.title, c.module, c.section, c.priority, trc.result, trc.defect, trc.comment,
                      trc.executed_at AS at, trc.executed_by AS who,
                      EXISTS (SELECT 1 FROM links l WHERE l.case_id = c.id) AS automated
               FROM test_run_cases trc JOIN cases c ON c.id = trc.case_id WHERE trc.test_run_id = ?""",
            (run["id"],)))
        for it in items:
            it["status"] = RESULT_TO_STATUS.get(it["result"], "Not Run")
    else:
        items = _rows(conn.execute(
            """SELECT c.id, c.title, c.module, c.section, c.priority, c.status, COALESCE(c.updated_at, c.last_run_at) AS at,
                      EXISTS (SELECT 1 FROM links l WHERE l.case_id = c.id) AS automated
               FROM cases c WHERE c.workspace_id = ?""", (ws_id,)))

    overall = _summarise(items)
    automated = sum(1 for it in items if it["automated"])
    overall["automated"] = automated
    overall["coverage_pct"] = _pct(automated, overall["total"])

    modules = {}
    for it in items:
        modules.setdefault(it["module"] or "(no module)", []).append(it)
    by_module = []
    for name, group in sorted(modules.items()):
        s = _summarise(group)
        s["module"] = name
        s["automated"] = sum(1 for it in group if it["automated"])
        s["coverage_pct"] = _pct(s["automated"], s["total"])
        by_module.append(s)

    by_priority = []
    for prio in ("High", "Medium", "Low"):
        group = [it for it in items if it["priority"] == prio]
        s = _summarise(group)
        s["priority"] = prio
        by_priority.append(s)

    order = {"High": 0, "Medium": 1, "Low": 2}
    failures = sorted((it for it in items if it["status"] in ("Fail", "Blocked", "Retest")),
                      key=lambda it: (order.get(it["priority"], 3), it["status"] != "Fail", it["module"] or "", it["id"]))

    high = [it for it in items if it["priority"] == "High"]
    gates = [
        {"name": f"At least {READY_EXECUTED_PCT}% of test cases executed",
         "value": f"{overall['executed_pct']}%", "ok": overall["executed_pct"] >= READY_EXECUTED_PCT},
        {"name": f"Pass rate at least {READY_PASS_PCT}%",
         "value": f"{overall['pass_rate']}%", "ok": overall["pass_rate"] >= READY_PASS_PCT},
        {"name": "No failed High-priority cases",
         "value": str(sum(it["status"] == "Fail" for it in high)), "ok": not any(it["status"] == "Fail" for it in high)},
        {"name": "No blocked High-priority cases",
         "value": str(sum(it["status"] == "Blocked" for it in high)), "ok": not any(it["status"] == "Blocked" for it in high)},
        {"name": "No cases waiting for retest",
         "value": str(overall["counts"]["Retest"]), "ok": overall["counts"]["Retest"] == 0},
    ]
    if all(g["ok"] for g in gates):
        verdict = "Ready"
    elif (gates[2]["ok"] and gates[3]["ok"] and overall["executed_pct"] >= AT_RISK_PCT
          and overall["pass_rate"] >= AT_RISK_PCT):
        verdict = "At risk"
    else:
        verdict = "Not ready"

    trend = _rows(conn.execute(
        """SELECT id, title, status, started_at, passed, failed + errors AS failed, skipped
           FROM runs WHERE workspace_id = ? AND status != 'Running'
             AND passed + failed + errors + skipped > 0 ORDER BY id DESC LIMIT 12""", (ws_id,)))
    trend.reverse()
    for t in trend:
        t["kind"] = "automation"
        t["pass_rate"] = _pct(t["passed"], t["passed"] + t["failed"])
    manual = list_runs(conn, ws_id)[:12]
    manual_trend = [{"id": m["id"], "title": m["name"], "status": m["status"], "started_at": m["created_at"],
                     "passed": m["passed"] or 0, "failed": (m["failed"] or 0) + (m["blocked"] or 0),
                     "untested": m["untested"] or 0, "total": m["total"],
                     "pass_rate": _pct(m["passed"] or 0, (m["passed"] or 0) + (m["failed"] or 0) + (m["blocked"] or 0) + (m["retest"] or 0)),
                     "executed_pct": _pct((m["total"] or 0) - (m["untested"] or 0), m["total"] or 0)}
                    for m in reversed(manual)]

    return {"scope": scope or "project", "run": dict(run) if run else None, "overall": overall,
            "by_module": by_module, "by_priority": by_priority, "failures": failures[:200],
            "failures_total": len(failures), "readiness": {"verdict": verdict, "gates": gates},
            "automation_trend": trend, "manual_trend": manual_trend}
