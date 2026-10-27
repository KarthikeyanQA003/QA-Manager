# CFlow Test Case Manager

A standalone local web tool for writing test cases, linking them to automation
tests in any connected project (CFlow pytest, Playwright JS/TS, Playwright
Python, pytest, Selenium Java), and running those tests. When a run finishes,
every linked case is set to Pass / Fail / Skipped from the results.

```bash
cd D:\test-case-manager
uv run server.py              # open http://localhost:8765
# or, without uv:  python server.py   (pip install openpyxl for Excel export)
```

In VS Code, open this folder and use *Terminal → Run Task → Test Case Manager:
start*.

The tool lives outside the projects it manages. It only reads their test files
and runs their test commands in their own folders.

## Sign-in and projects

**Email and password sign-in**, set up entirely in the UI:

1. **First start:** a *Create your account* screen appears. The first account
   becomes the **admin**, and its email domain (e.g. `cavintek.com`) becomes the
   only domain allowed to register.
2. **After that:** the *Sign in* screen has a *Create an account* link. Anyone
   with an email on the allowed domain can register.
3. **Change password:** use the avatar menu (top right).

How sign-in is protected:

- Passwords are stored only as salted PBKDF2-SHA256 hashes.
- After 5 wrong passwords, the account is locked for 5 minutes.
- Sessions last 7 days and are stored in an HttpOnly, SameSite=Strict cookie.
- The allowed domains are saved in `data/config.json`. Edit that file and
  restart the server to add another domain.

**Optional Google sign-in:** add `"google_client_id": "....apps.googleusercontent.com"`
to `data/config.json` (a Web application OAuth client, with
`http://localhost:8765` as an authorised JavaScript origin) and restart. A
*Sign in with Google* button then appears under the password form. Google
accounts must be verified, on the allowed domain, and part of that domain's
Google Workspace.

**Projects (Jira-style).** After sign-in you pick a project, or create one.
Each project has its own test cases, board, automation connections, runs and
dashboard. *Create project* asks for:

- name and key
- description
- application URL
- environments (offered in the Run dialog)
- team members (company emails)
- the automation framework and folder

Its tests are scanned and, if you leave the box ticked, each one becomes a
linked test case. The project's **Overview** is its landing page. It shows the
project details, pass rate, last run, and *Run all*, *Open test cases* and
*Edit project*.

Access rules: a project is visible to its owner, its team members and admins.
Only the owner or an admin can change the team. Data from before sign-in
existed became the **CFlow** project, and the first person to open it becomes
its owner.

**Local testing only:** `uv run server.py --dev-user you@company.com` adds a
*Developer sign-in* button for that one address, with no password. Anyone who
can open the page can then sign in as that user, so don't use it for normal
work.

- **Test Cases**: create or edit cases (title, module, priority, steps, expected
  result). Link automation tests in the editor. A case with no links is manual,
  and you set its status by hand.
- **Automation**: every test found in the connected projects, with its last
  result and linked cases. Use *Create cases from selected* to make one linked
  case per test. Use *Re-scan tests* after adding or renaming tests.
- **Run**: from either tab. Choose Headed or Headless. Only one run can go at a
  time, with a single worker.
- **Live progress**: every test case updates as soon as its test finishes. You
  don't have to wait for the whole run.
  - A strip at the top of every page shows what is running now, the progress
    and the pass/fail counts.
  - The Dashboard and Board refresh every few seconds. A case whose test is
    executing sits in the **Running** column.
  - *View live* lists every test with its result, start and finish time. Your
    console output is under *Console output*.
  - For pytest-based projects this works through a small pytest plugin,
    `live/tcm_live.py`, which the tool loads automatically. Your project needs
    no changes. Playwright JS results come from its list reporter. Maven
    results arrive when the run ends.
- **HTML report**: every run gets a self-contained report. It shows a summary,
  pass rate, a per-module breakdown and every test with its linked TC ids,
  start and finish times, duration and failure details. You can filter and
  search it. Open it with *HTML report* in the run window or *Report* on the
  live strip. A copy is saved as `data/runs/run_<id>_report.html` when the run
  ends.
- **Times** are all India Standard Time and shown as `27-10-2026 02:35:10 PM IST`.

How a case's status is set from a run: if any linked test failed or errored,
the case is **Fail**. If all linked tests that ran passed, it is **Pass**. If
some were skipped and the rest passed, it is **Blocked**. If all were skipped,
it is **Skipped**. Only cases linked to tests in that run are changed.

You can also record runs made outside the tool (CI, `run_tests.py`):

```bash
# in the CFlow repo
uv run pytest TestCases/... --junitxml=results.xml
# in this folder (--project is the id shown on the Projects tab; default 1)
uv run server.py --import-junit D:\Automation\Cflow\testautomation\results.xml --project 1
```

Data is stored in `data/` (SQLite and run logs), which is git-ignored. Back up
`data/tcm.db` to keep your cases.

## Jira-style views, IDE links and exports

- **Board**: a column per status. Drag a card to another column to change its
  status. Click a card to open it.
- **Module sidebar** (Test Cases tab): filter the cases by module.
- **Open in IDE**: automation tests, linked tests and run results have an
  "Open in IDE" link (`vscode://file/...:line`) that opens the file at the test
  in VS Code. In VS Code, use *Terminal → Run Task → Test Case Manager: start*
  to start the server.
- **Import all automation tests** (Test Cases tab): creates one linked case for
  every automation test that has none. Running it again is safe; tests that
  already have a case are skipped.
- **Export** (Test Cases tab):
  - Excel / CSV: every case with its steps, status and linked tests.
  - Playwright Python / Playwright JavaScript / Selenium Java (TestNG + Maven):
    a zip project with one test per case. Each test is named after its TC id,
    has the steps and expected result as comments, and is marked pending until
    you implement it. `test_case_map.csv` in the zip links each case to its
    generated test and to the existing CFlow pytest test.

## Connecting existing automation projects (any framework)

Use the **Projects** tab and click **+ Connect project**. Enter a name, pick the
framework, and paste the project folder path (in VS Code: right-click the
folder → *Copy Path*). The tool scans the folder for tests. Then use
**Import as cases** to create one linked test case per test.

| Framework | Tests it finds | Default run command (runs in the project folder) |
|---|---|---|
| Playwright JS / TS | `test()` inside `test.describe()` in `*.spec/test.(js\|ts)` | `npx playwright test file:line … --reporter=list,junit` |
| Playwright Python | `test_*` functions and methods | `python -m pytest <ids> --headed --junitxml=…` |
| pytest (Selenium Python, other) | `test_*` functions and methods | `python -m pytest <ids> --junitxml=…` |
| Selenium Java (Maven) | `@Test` methods (TestNG, JUnit 4/5) under `src/test/java` | `mvn test -Dtest=Class#m1+m2` (reads `target/surefire-reports`) |

- Change the run command per project if your project uses another runner (for
  example a specific Python executable, Gradle or a Maven profile). You can use
  these placeholders: `{targets}`, `{junit}`, `{headed}`, `{headless}`,
  `{headless_bool}` and `{region}`.
- When a run includes tests from several projects, the projects run one after
  another. Every project's results update its linked cases.
- **Open in IDE** opens each test in its own project folder.
- To record results from a run you made outside the tool:
  `server.py --import-junit results.xml --project <id>`.
- **Security:** run commands are shell commands. The server listens only on
  127.0.0.1 and accepts API calls only from its own page, as JSON. Requests
  from other websites open in your browser are refused.
