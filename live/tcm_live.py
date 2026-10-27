"""
pytest plugin loaded into every pytest run the Test Case Manager starts (via
PYTEST_PLUGINS=tcm_live). It appends one JSON line per event to the file named
by TCM_LIVE_FILE so the tool can show each test as it starts and finishes,
instead of waiting for the junit XML at the end.

    {"event": "start",  "nodeid": "...", "t": 1730000000.0}
    {"event": "result", "nodeid": "...", "outcome": "passed|failed|error|skipped",
     "duration": 1.2, "message": "...", "t": ...}
    {"event": "rerun",  "nodeid": "...", "t": ...}

Only the controller process writes: under pytest-xdist the workers' reports are
relayed to the controller's hooks, so writing there too would double them.
"""

import json
import os
import threading
import time

_lock = threading.Lock()
_state = {}
_on = {"value": False}


def _enabled(config):
    return bool(os.environ.get("TCM_LIVE_FILE")) and not hasattr(config, "workerinput")


def _emit(**event):
    event["t"] = time.time()
    line = json.dumps(event, ensure_ascii=False)
    with _lock, open(os.environ["TCM_LIVE_FILE"], "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def pytest_configure(config):
    _on["value"] = _enabled(config)


def pytest_runtest_logstart(nodeid, location):
    if not _on["value"]:
        return
    _state[nodeid] = {"outcome": None, "duration": 0.0, "message": ""}
    _emit(event="start", nodeid=nodeid)


def pytest_runtest_logreport(report):
    if not _on["value"]:
        return
    st = _state.setdefault(report.nodeid, {"outcome": None, "duration": 0.0, "message": ""})
    st["duration"] += getattr(report, "duration", 0.0) or 0.0
    if report.outcome == "rerun":
        _state.pop(report.nodeid, None)
        _emit(event="rerun", nodeid=report.nodeid)
        return
    if st["outcome"] is None or st["outcome"] == "passed":
        if report.failed:
            st["outcome"] = "failed" if report.when == "call" else "error"
            st["message"] = _message(report)
        elif report.skipped:
            st["outcome"] = "skipped"
            st["message"] = _message(report)
        elif report.when == "call":
            st["outcome"] = "passed"
    if report.when == "teardown":
        _state.pop(report.nodeid, None)
        _emit(event="result", nodeid=report.nodeid, outcome=st["outcome"] or "passed",
              duration=round(st["duration"], 2), message=st["message"])


def _message(report):
    if report.skipped and isinstance(report.longrepr, tuple):
        return str(report.longrepr[2])[:2000]
    crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
    if crash is not None:
        return str(crash.message)[:2000]
    return str(getattr(report, "longreprtext", "") or "")[:2000]
