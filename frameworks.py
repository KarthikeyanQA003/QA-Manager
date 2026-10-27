"""
Framework adapters: how to find the tests in a project, how to run a chosen
subset, and how to read the run's junit XML back onto those tests.

Every adapter discovers tests statically (no imports, no node/mvn calls), so a
scan is fast and safe on any checkout. Each discovered test carries:

    file       path relative to the project root (forward slashes)
    suite      grouping shown in the tool (folder, or spec/class name)
    cls, name  class / describe path and test name
    line       1-based line of the test, for the IDE link and file:line runs
    target     what the framework's CLI accepts to select this one test
    junit_key  how this test appears in the framework's junit XML
"""

import ast
import os
import re
import shlex
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "site-packages",
             "target", "build", "dist", ".idea", ".vscode", "playwright-report", "test-results",
             ".pytest_cache", "Reports"}

# label, default run command. Placeholders: {targets} {junit} {headed} {headless}
# {region}. {junit} is the XML file the run must write (Maven ignores it and
# writes target/surefire-reports, which is collected instead).
FRAMEWORKS = {
    "cflow-pytest": {
        "label": "CFlow pytest (uv run, region option)",
        "command": 'uv run pytest {targets} -o "addopts=--browser_name=chromium -v -s '
                   '--capture=tee-sys --region={region}{headless}" -p no:cacheprovider --junitxml="{junit}"',
    },
    "playwright-python": {
        "label": "Playwright Python (pytest)",
        "command": 'python -m pytest {targets} -v {headed} --junitxml="{junit}"',
    },
    "pytest": {
        "label": "pytest (Selenium Python / other)",
        "command": 'python -m pytest {targets} -v --junitxml="{junit}"',
    },
    "playwright-js": {
        "label": "Playwright JS / TS",
        "command": "npx playwright test {targets} --reporter=list,junit {headed}",
    },
    "selenium-maven": {
        "label": "Selenium Java (Maven: TestNG / JUnit)",
        "command": 'mvn test -Dtest="{targets}" -Dsurefire.failIfNoSpecifiedTests=false '
                   "-DfailIfNoTests=false -Dheadless={headless_bool}",
    },
}


def walk(root, patterns):
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if any(re.fullmatch(p, fn) for p in patterns):
                yield Path(dirpath) / fn


def rel(path, root):
    return Path(path).relative_to(root).as_posix()


# ---------------------------------------------------------------------------
# Python (pytest family)
# ---------------------------------------------------------------------------

def _marker_names(decorators):
    names = []
    for dec in decorators:
        node = dec.func if isinstance(dec, ast.Call) else dec
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        parts.reverse()
        if len(parts) >= 3 and parts[:2] == ["pytest", "mark"]:
            label = parts[2]
            if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                label += f"({dec.args[0].value})"
            names.append(label)
    return names


def discover_python(root, scan_dir, suite_prefix=""):
    found = []
    base = Path(root) / scan_dir if scan_dir else Path(root)
    for path in sorted(walk(base, [r"test_.*\.py", r".*_test\.py"])):
        r = rel(path, root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=r)
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"[WARN] skipped {r}: {exc}")
            continue
        module = r[:-3].replace("/", ".")
        suite = suite_prefix + path.parent.name

        def add(fn, cls=None, cls_markers=()):
            node = f"{r}::{cls}::{fn.name}" if cls else f"{r}::{fn.name}"
            found.append({
                "file": r, "suite": suite, "cls": cls or "", "name": fn.name, "line": fn.lineno,
                "doc": (ast.get_docstring(fn) or "").strip(),
                "markers": ", ".join(list(cls_markers) + _marker_names(fn.decorator_list)),
                "target": node,
                "junit_key": f"{module}.{cls}::{fn.name}" if cls else f"{module}::{fn.name}",
            })

        for item in tree.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                add(item)
            elif isinstance(item, ast.ClassDef) and item.name.startswith("Test"):
                cls_markers = _marker_names(item.decorator_list)
                for sub in item.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                        add(sub, item.name, cls_markers)
    return found


# ---------------------------------------------------------------------------
# Playwright JS / TS
# ---------------------------------------------------------------------------

_STR = r"""(?P<q>['"`])(?P<title>(?:\\.|(?!(?P=q)).)*)(?P=q)"""
_DESCRIBE = re.compile(r"\b(?:test\.)?describe(?:\.(?:only|skip|fixme|serial|parallel))?\s*\(\s*" + _STR)
_TEST = re.compile(r"(?<![.\w])(?:test|it)(?:\.(?P<mod>only|skip|fixme|fail|slow))?\s*\(\s*" + _STR)


def _strip_strings_and_comments(line):
    line = re.sub(r"//.*$", "", line)
    return re.sub(r"""(['"`])(?:\\.|(?!\1).)*\1""", '""', line)


def discover_playwright_js(root, scan_dir, suite_prefix=""):
    """Brace-depth tracking is enough for the usual describe/test nesting."""
    found = []
    base = Path(root) / scan_dir if scan_dir else Path(root)
    for path in sorted(walk(base, [r".*\.(spec|test)\.(js|ts|mjs|cjs|jsx|tsx)"])):
        r = rel(path, root)
        stack, depth, in_block_comment = [], 0, False
        for lineno, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            line = re.sub(r"/\*.*?\*/", "", raw)       # inline /* ... */
            if in_block_comment:
                if "*/" not in line:
                    continue
                line, in_block_comment = line.split("*/", 1)[1], False
            if "/*" in line and "*/" not in line.split("/*", 1)[1]:
                line, in_block_comment = line.split("/*", 1)[0], True

            d = _DESCRIBE.search(line)
            t = None if d else _TEST.search(line)
            if d:
                stack.append((depth, d.group("title")))
            elif t and t.group("mod") not in ("describe",):
                titles = [s[1] for s in stack] + [t.group("title")]
                found.append({
                    "file": r, "suite": suite_prefix + path.name, "cls": " › ".join(s[1] for s in stack),
                    "name": t.group("title"), "line": lineno, "doc": "",
                    "markers": t.group("mod") or "",
                    "target": f"{r}:{lineno}",
                    "junit_key": f"{path.name}::{' › '.join(titles)}",
                })
            code = _strip_strings_and_comments(line)
            depth += code.count("{") - code.count("}")
            while stack and depth <= stack[-1][0]:
                stack.pop()
    return found


# ---------------------------------------------------------------------------
# Selenium Java (TestNG / JUnit 4 / JUnit 5)
# ---------------------------------------------------------------------------

_PKG = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_CLASS = re.compile(r"\b(?:public\s+)?(?:final\s+|abstract\s+)*class\s+(\w+)")
_METHOD = re.compile(r"\b(?:public|protected|private)?\s*(?:static\s+)?void\s+(\w+)\s*\(")
_DESC = re.compile(r"""(?:description|displayName)\s*=\s*"((?:\\.|[^"\\])*)\"""")
_DISPLAY = re.compile(r"""@DisplayName\s*\(\s*"((?:\\.|[^"\\])*)\"""")


def discover_java(root, scan_dir, suite_prefix=""):
    found = []
    base = Path(root) / (scan_dir or "src/test/java")
    if not base.exists():
        base = Path(root)
    for path in sorted(walk(base, [r".*\.java"])):
        r = rel(path, root)
        text = path.read_text(encoding="utf-8", errors="replace")
        if "@Test" not in text:
            continue
        pkg = (_PKG.search(text) or [None, ""])[1]
        lines = text.splitlines()
        cls_name, pending, ann_text = None, False, ""
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if cls_name is None:
                m = _CLASS.search(stripped)
                if m and not stripped.startswith(("*", "//")):
                    cls_name = m.group(1)
            if re.match(r"@(org\.testng\.annotations\.|org\.junit\.(jupiter\.api\.)?)?Test\b", stripped):
                pending, ann_text = True, stripped
                continue
            if pending:
                if stripped.startswith("@"):
                    ann_text += " " + stripped
                    continue
                m = _METHOD.search(stripped)
                if m:
                    fqn = f"{pkg}.{cls_name}" if pkg else cls_name
                    desc = _DESC.search(ann_text) or _DISPLAY.search(ann_text)
                    found.append({
                        "file": r, "suite": suite_prefix + (cls_name or path.stem), "cls": cls_name or path.stem,
                        "name": m.group(1), "line": lineno, "doc": desc.group(1) if desc else "",
                        "markers": "disabled" if "enabled = false" in ann_text.replace("enabled=false", "enabled = false") else "",
                        "target": f"{cls_name}#{m.group(1)}",
                        "junit_key": f"{fqn}::{m.group(1)}",
                    })
                    pending, ann_text = False, ""
                elif stripped and not stripped.startswith(("//", "*", "/*")):
                    ann_text += " " + stripped
    return found


DISCOVER = {
    "cflow-pytest": discover_python,
    "playwright-python": discover_python,
    "pytest": discover_python,
    "playwright-js": discover_playwright_js,
    "selenium-maven": discover_java,
}


def discover(framework, root, scan_dir="", suite_prefix=""):
    return DISCOVER[framework](root, scan_dir, suite_prefix)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _compact_paths(tests, universe):
    """Fewest file/folder paths that select exactly `tests`.

    A whole suite is hundreds of test ids -- far past the Windows command-line
    limit (~8k chars) -- so any file whose every test is selected collapses to
    the file path, and any folder whose every test is selected to the folder.
    """
    chosen = {t["node_key"] for t in tests}
    by_file = {}
    for t in universe:
        by_file.setdefault(t["file"], []).append(t)
    full_files = {f for f, ts in by_file.items() if all(t["node_key"] in chosen for t in ts)}

    # Folders: every test under the folder is selected.
    def folder_of(f):
        return f.rsplit("/", 1)[0] if "/" in f else ""

    folders = {}
    for f in by_file:
        d = folder_of(f)
        while d:
            folders.setdefault(d, set()).add(f)
            d = folder_of(d)
    full_dirs = {d for d, fs in folders.items() if fs <= full_files}
    # keep only the outermost full folders
    full_dirs = {d for d in full_dirs if not any(d != o and d.startswith(o + "/") for o in full_dirs)}

    targets, done = [], set()
    for d in sorted(full_dirs):
        targets.append(d)
        done |= folders[d]
    for f in sorted(full_files - done):
        targets.append(f)
        done.add(f)
    for t in tests:
        if t["file"] not in done:
            targets.append(t["target"])
    return targets


def build_targets(framework, tests, universe=None):
    """CLI selection for `tests`; `universe` is every test in the project, used to
    shorten whole files / folders / classes to one path or name."""
    universe = universe or tests
    for t in tests + universe:
        t.setdefault("node_key", t["target"])
    if framework == "selenium-maven":
        # Surefire syntax: Class#m1+m2,Other,...  (a fully selected class runs whole)
        chosen = {t["node_key"] for t in tests}
        per_cls_all = {}
        for t in universe:
            per_cls_all.setdefault(t["target"].split("#", 1)[0], []).append(t)
        per_cls = {}
        for t in tests:
            cls, method = t["target"].split("#", 1)
            per_cls.setdefault(cls, []).append(method)
        parts = []
        for c, ms in per_cls.items():
            whole = all(t["node_key"] in chosen for t in per_cls_all.get(c, []))
            parts.append(c if whole else f"{c}#{'+'.join(ms)}")
        return ",".join(parts)
    # pytest node ids / Playwright file:line, or whole files and folders
    return " ".join(_quote(x) for x in _compact_paths(tests, universe))


def _quote(arg):
    if os.name == "nt":
        return f'"{arg}"' if re.search(r"[\s&|<>^]", arg) else arg
    return shlex.quote(arg)


def build_command(framework, template, tests, junit, headless, region, universe=None):
    return (template or FRAMEWORKS[framework]["command"]).format(
        targets=build_targets(framework, tests, universe),
        junit=junit,
        headed="" if headless else "--headed",
        headless=" --headless" if headless else "",
        headless_bool="true" if headless else "false",
        region=region,
    )


LIVE_PLUGIN_DIR = Path(__file__).resolve().parent / "live"
PYTEST_FAMILY = ("cflow-pytest", "playwright-python", "pytest")


def run_env(framework, junit, live_file=None):
    env = {}
    if framework == "playwright-js":
        env["PLAYWRIGHT_JUNIT_OUTPUT_NAME"] = str(junit)
    if framework in PYTEST_FAMILY and live_file:
        # live/tcm_live.py reports each test as it starts and finishes.
        plugins = [p for p in os.environ.get("PYTEST_PLUGINS", "").split(",") if p.strip()]
        env["PYTEST_PLUGINS"] = ",".join(plugins + ["tcm_live"])
        env["PYTHONPATH"] = os.pathsep.join(
            [str(LIVE_PLUGIN_DIR)] + [p for p in [os.environ.get("PYTHONPATH", "")] if p])
        env["TCM_LIVE_FILE"] = str(live_file)
    return env


# Playwright list reporter result line, e.g.
#   "  ok 3 [chromium] › tests/login.spec.ts:12:5 › Login › signs in (2.1s)"
# (Windows consoles get ok / x; others get ✓ / ✘; "-" is skipped.)
_PW_LINE = re.compile(
    r"^\s*(?P<mark>ok|x|✓|✘|-|°)\s+\d+\s+(?:\[[^\]]*\]\s+›\s+)?(?P<file>[^\s:]+):\d+:\d+\s+›\s+"
    r"(?P<title>.+?)(?:\s+\((?:retry #\d+ )?[\d.]+m?s\)|\s+\(\d+(?:\.\d+)?m\))?\s*$")


def parse_live_line(framework, line):
    """(key, outcome) for a log line that reports a finished test, else None.
    Used for frameworks without the pytest plugin (Playwright JS)."""
    if framework != "playwright-js":
        return None
    m = _PW_LINE.match(line)
    if not m:
        return None
    outcome = {"ok": "passed", "✓": "passed", "x": "failed", "✘": "failed"}.get(m["mark"], "skipped")
    spec_file = re.split(r"[\\/]", m["file"])[-1]
    return f"{spec_file}::{m['title'].strip()}", outcome


def result_files(framework, project_root, junit, started_ts):
    """junit XML files a run produced."""
    if framework == "selenium-maven":
        reports = Path(project_root) / "target" / "surefire-reports"
        return [p for p in reports.glob("TEST-*.xml") if p.stat().st_mtime >= started_ts - 1]
    return [Path(junit)] if Path(junit).exists() else []


def junit_key(framework, classname, name):
    """Turn a junit <testcase> into the key discovery recorded for that test."""
    if framework == "playwright-js":
        # classname is the spec path relative to testDir; match on the file name.
        spec_file = re.split(r"[\\/]", classname)[-1]
        return f"{spec_file}::{name}"
    name = re.sub(r"\[.*\]$", "", name)          # pytest parametrize ids
    name = re.sub(r"\(.*\)$", "", name)          # JUnit5 method(params)
    return f"{classname}::{name}"
