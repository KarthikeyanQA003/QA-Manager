"""
HTML run report -- one self-contained page per run (no external files), built
from what the tool recorded: every test in the run, its result, its start and
finish time (India Standard Time) and the test cases it updated.

Written to data/runs/run_<id>_report.html when a run ends, and served at
/api/runs/<id>/report at any time (also while the run is still going).
"""

import json
from datetime import datetime
from html import escape

OUTCOMES = [("passed", "Passed", "#1f9d55"), ("failed", "Failed", "#d64545"), ("error", "Error", "#b83280"),
            ("skipped", "Skipped", "#c98a00"), ("running", "Running", "#2f6fde"),
            ("queued", "Queued", "#8a92a3"), ("not run", "Not run", "#8a92a3")]
COLOR = {k: c for k, _, c in OUTCOMES}
LABEL = {k: l for k, l, _ in OUTCOMES}


def ist(stamp):
    """'2026-10-27 14:05:09' -> '27-10-2026 02:05:09 PM IST'."""
    if not stamp:
        return ""
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").strftime("%d-%m-%Y %I:%M:%S %p") + " IST"
    except ValueError:
        return stamp


def span(start, end):
    try:
        secs = int((datetime.strptime(end, "%Y-%m-%d %H:%M:%S") -
                    datetime.strptime(start, "%Y-%m-%d %H:%M:%S")).total_seconds())
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(max(secs, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def tc_id(case_id):
    return f"TC-{int(case_id):04d}"


def build(conn, run_id, generated_at):
    run = dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    ws = conn.execute("SELECT key, name FROM workspaces WHERE id=?", (run["workspace_id"],)).fetchone()
    results = [dict(r) for r in conn.execute(
        """SELECT r.node_id, r.outcome, r.duration, r.message, r.started_at, r.finished_at,
                  a.file, a.cls, a.name, a.suite, a.line
           FROM run_results r LEFT JOIN automation_tests a ON a.node_id = r.node_id
           WHERE r.run_id=? ORDER BY COALESCE(r.started_at, '9999'), r.rowid""", (run_id,))]
    cases = {}
    for r in conn.execute(
            """SELECT l.node_id, c.id, c.title, c.status FROM links l JOIN cases c ON c.id = l.case_id
               JOIN run_results r ON r.node_id = l.node_id AND r.run_id = ?""", (run_id,)):
        cases.setdefault(r["node_id"], []).append(dict(r))
    try:
        opts = json.loads(run["options"] or "{}")
    except ValueError:
        opts = {}

    counts = {k: 0 for k, _, _ in OUTCOMES}
    for r in results:
        counts[r["outcome"] if r["outcome"] in counts else "not run"] += 1
    total = len(results)
    executed = counts["passed"] + counts["failed"] + counts["error"]
    pass_rate = round(100 * counts["passed"] / executed) if executed else 0
    end = run["finished_at"] or generated_at

    modules = {}
    for r in results:
        m = modules.setdefault(r["suite"] or "-", {k: 0 for k in counts} | {"total": 0})
        m["total"] += 1
        m[r["outcome"] if r["outcome"] in counts else "not run"] += 1

    def chip(outcome):
        return (f'<span class="chip" style="background:{COLOR.get(outcome, "#8a92a3")}">'
                f'{escape(LABEL.get(outcome, outcome or ""))}</span>')

    def bar(c, n):
        if not n:
            return '<div class="bar"></div>'
        return '<div class="bar">' + "".join(
            f'<span style="width:{100 * c[k] / n:.2f}%;background:{COLOR[k]}" title="{LABEL[k]}: {c[k]}"></span>'
            for k, _, _ in OUTCOMES if c.get(k)) + "</div>"

    cards = [("Total tests", total, "#0b3d91"), ("Passed", counts["passed"], COLOR["passed"]),
             ("Failed", counts["failed"] + counts["error"], COLOR["failed"]),
             ("Skipped", counts["skipped"], COLOR["skipped"]),
             ("Not run", counts["not run"] + counts["queued"] + counts["running"], COLOR["not run"]),
             ("Pass rate", f"{pass_rate}%", COLOR["passed"] if pass_rate >= 90 else COLOR["skipped"])]

    rows_html = []
    for i, r in enumerate(results, 1):
        name = f'{r["cls"] + " › " if r["cls"] else ""}{r["name"] or r["node_id"]}'
        linked = " ".join(f'<span class="tc" title="{escape(c["title"])}">{tc_id(c["id"])}</span>'
                          for c in cases.get(r["node_id"], [])) or '<span class="muted">—</span>'
        msg = (r["message"] or "").strip()
        detail = (f'<details><summary>{escape(msg.splitlines()[0][:160])}</summary><pre>{escape(msg)}</pre></details>'
                  if msg else "")
        rows_html.append(
            f'<tr data-o="{escape(r["outcome"] or "")}"><td class="muted">{i}</td>'
            f'<td><div class="tn">{escape(name)}</div><div class="muted mono">{escape(r["file"] or r["node_id"])}'
            f'{":" + str(r["line"]) if r["line"] else ""}</div>{detail}</td>'
            f'<td>{escape(r["suite"] or "")}</td><td>{linked}</td><td>{chip(r["outcome"])}</td>'
            f'<td class="nw">{ist(r["started_at"])}</td><td class="nw">{ist(r["finished_at"]) if r["outcome"] not in ("queued", "running") else ""}</td>'
            f'<td class="nw">{(r["duration"] or 0):.1f}s</td></tr>')

    module_rows = "".join(
        f'<tr><td>{escape(name)}</td><td>{m["total"]}</td><td>{m["passed"]}</td><td>{m["failed"] + m["error"]}</td>'
        f'<td>{m["skipped"]}</td><td>{m["not run"] + m["queued"] + m["running"]}</td><td>{bar(m, m["total"])}</td></tr>'
        for name, m in sorted(modules.items()))

    status_color = {"Passed": COLOR["passed"], "Failed": COLOR["failed"], "Running": COLOR["running"]}.get(
        run["status"], "#8a92a3")
    meta = [("Project", f'{escape(ws["name"])} ({escape(ws["key"])})' if ws else ""),
            ("Status", f'<span class="chip" style="background:{status_color}">{escape(run["status"])}</span>'),
            ("Started", ist(run["started_at"])), ("Finished", ist(run["finished_at"]) or "still running"),
            ("Duration", span(run["started_at"], end)),
            ("Mode", "Headless" if opts.get("headless") else "Headed"), ("Region", escape(opts.get("region") or "")),
            ("Started by", escape(opts.get("started_by") or "")), ("Report generated", ist(generated_at))]

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run #{run_id} report</title>
<style>
:root {{ --ink:#14213d; --muted:#5b6b86; --line:#d6e0f0; --bg:#eef3fa; --brand:#0b3d91; --accent:#1a5fd0; --tint:#e6eefb; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; font:14px/1.45 Arial,"Helvetica Neue",Helvetica,sans-serif; color:var(--ink); background:var(--bg); }}
header {{ background:linear-gradient(90deg,#0b3d91,#1552b8); color:#fff; padding:18px 24px; border-bottom:3px solid var(--accent); }}
header h1 {{ margin:0; font-size:20px; }} header .sub {{ color:#cfdcf5; margin-top:2px; }}
main {{ max-width:1400px; margin:0 auto; padding:20px 16px; }}
.panel {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:16px; margin-bottom:16px; }}
.meta {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px 20px; margin:0; }}
.meta dt {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.03em; }} .meta dd {{ margin:0; font-weight:600; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:16px; }}
.card {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:14px; border-top:4px solid; }}
.card .n {{ font-size:26px; font-weight:700; }} .card .l {{ color:var(--muted); font-size:12px; text-transform:uppercase; }}
.bar {{ display:flex; height:10px; border-radius:5px; overflow:hidden; background:#eceef2; min-width:120px; }}
.bar span {{ display:block; height:100%; }}
h2 {{ font-size:15px; margin:0 0 12px; }}
table {{ width:100%; border-collapse:collapse; }}
th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ font-size:12px; text-transform:uppercase; color:var(--brand); font-weight:700; background:var(--tint); border-bottom:2px solid #c3d4f0; }}
.chip {{ display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; font-weight:600; color:#fff; white-space:nowrap; }}
.muted {{ color:var(--muted); }} .mono {{ font-family:ui-monospace,Consolas,monospace; font-size:11px; word-break:break-all; }}
.nw {{ white-space:nowrap; }} .tn {{ font-weight:600; word-break:break-word; }}
.tc {{ display:inline-block; background:#eef1f6; border-radius:10px; padding:0 7px; margin:0 3px 3px 0; font-size:12px; }}
details summary {{ cursor:pointer; color:#d64545; font-size:12px; margin-top:4px; }}
details pre {{ background:#11151c; color:#d8dee9; padding:10px; border-radius:6px; white-space:pre-wrap; font-size:12px; max-height:320px; overflow:auto; }}
.filters {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; align-items:center; }}
.filters button {{ font:inherit; padding:4px 12px; border:1px solid var(--line); border-radius:14px; background:#fff; cursor:pointer; }}
.filters button.on {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
.filters input {{ font:inherit; padding:5px 9px; border:1px solid var(--line); border-radius:6px; margin-left:auto; min-width:220px; }}
.scroll {{ overflow-x:auto; }}
@media print {{ .filters {{ display:none; }} details {{ display:block; }} body {{ background:#fff; }} }}
</style></head>
<body>
<header><h1>Run #{run_id} — {escape(run["title"] or "")}</h1>
<div class="sub">Test execution report · all times India Standard Time (IST)</div></header>
<main>
<section class="panel"><dl class="meta">{"".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in meta)}</dl></section>
<div class="cards">{"".join(f'<div class="card" style="border-top-color:{c}"><div class="n">{v}</div><div class="l">{k}</div></div>' for k, v, c in cards)}</div>
<section class="panel"><h2>Overall</h2>{bar(counts, total)}</section>
<section class="panel scroll"><h2>By module</h2>
<table><thead><tr><th>Module</th><th>Total</th><th>Passed</th><th>Failed</th><th>Skipped</th><th>Not run</th><th style="width:30%">Result</th></tr></thead>
<tbody>{module_rows}</tbody></table></section>
<section class="panel scroll"><h2>Test executions</h2>
<div class="filters">
  <button class="on" data-f="">All ({total})</button><button data-f="passed">Passed ({counts["passed"]})</button>
  <button data-f="failed error">Failed ({counts["failed"] + counts["error"]})</button>
  <button data-f="skipped">Skipped ({counts["skipped"]})</button>
  <button data-f="not run queued running">Not run ({counts["not run"] + counts["queued"] + counts["running"]})</button>
  <input id="q" type="search" placeholder="Search tests or TC ids">
</div>
<table><thead><tr><th>#</th><th>Test</th><th>Module</th><th>Test cases</th><th>Result</th><th>Started (IST)</th><th>Finished (IST)</th><th>Duration</th></tr></thead>
<tbody id="rows">{"".join(rows_html) or '<tr><td colspan="8" class="muted">No tests recorded.</td></tr>'}</tbody></table>
</section>
</main>
<script>
let f = "";
const apply = () => {{
  const q = document.getElementById("q").value.toLowerCase();
  document.querySelectorAll("#rows tr[data-o]").forEach(tr => {{
    tr.style.display = (!f || f.split(" ").includes(tr.dataset.o)) && (!q || tr.textContent.toLowerCase().includes(q)) ? "" : "none";
  }});
}};
document.querySelectorAll(".filters button").forEach(b => b.onclick = () => {{
  document.querySelectorAll(".filters button").forEach(x => x.classList.toggle("on", x === b));
  f = b.dataset.f; apply();
}});
document.getElementById("q").oninput = apply;
</script>
</body></html>
"""
