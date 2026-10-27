"""
Export test cases as a ready-to-open automation project, one test per case.

    pw-py         Playwright + pytest (Python)
    pw-js         Playwright Test (JavaScript)
    selenium-java Selenium WebDriver + TestNG (Java, Maven)

Every generated test is named after its case (TC-0001 -> test_tc_0001_...),
carries the case's steps / expected result as comments, and is marked
not-implemented so a fresh project runs green-but-pending until filled in.
test_case_map.csv links each case to its generated test and to the existing
CFlow pytest test(s) it is already linked to.
"""

import csv
import io
import re
import zipfile

FORMATS = {
    "pw-py": "Playwright Python",
    "pw-js": "Playwright JavaScript",
    "selenium-java": "Selenium Java",
}

APP_URL = "https://testapp.cflowapps.com"


def tc(case_id):
    return f"TC-{case_id:04d}"


def slug(text, limit=60):
    s = re.sub(r"[^0-9a-zA-Z]+", "_", text or "").strip("_").lower()
    return (s[:limit].rstrip("_")) or "case"


def pascal(text):
    words = re.split(r"[^0-9a-zA-Z]+", text or "")
    name = "".join(w[:1].upper() + w[1:] for w in words if w) or "General"
    return name if name[0].isalpha() else "M" + name


def lines(text):
    return [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]


def comment_block(case, prefix, indent):
    """Title / preconditions / steps / expected / existing automation as comments."""
    pad = " " * indent
    out = [f"{pad}{prefix} {tc(case['id'])}: {case['title']}",
           f"{pad}{prefix} Module: {case['module'] or '-'} | Priority: {case['priority']} | Status: {case['status']}"]
    for label, key in (("Preconditions", "preconditions"), ("Steps", "steps"), ("Expected", "expected")):
        body = lines(case.get(key))
        if body:
            out.append(f"{pad}{prefix} {label}:")
            out += [f"{pad}{prefix}   {ln}" for ln in body]
    for node in case["links"]:
        out.append(f"{pad}{prefix} Existing CFlow automation: {node}")
    return "\n".join(out)


def js_str(s):
    return (s or "").replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")


def java_str(s):
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def by_module(cases):
    groups = {}
    for c in cases:
        groups.setdefault(c["module"] or "General", []).append(c)
    return groups


# ---------------------------------------------------------------------------
# Playwright Python
# ---------------------------------------------------------------------------

def gen_pw_py(cases):
    files, mapping = {}, []
    files["requirements.txt"] = "pytest\npytest-playwright\n"
    files["pytest.ini"] = (
        "[pytest]\n"
        f"base_url = {APP_URL}\n"
        "addopts = -v --headed\n"
        "markers =\n    tc(id): test case id from the CFlow Test Case Manager\n"
    )
    files["README.md"] = (
        "# CFlow test cases -- Playwright Python\n\n"
        "```bash\npip install -r requirements.txt\nplaywright install\npytest\n```\n\n"
        "Each test is pending (`pytest.skip`) until implemented. `test_case_map.csv` links\n"
        "every test back to its TC id and to the existing CFlow pytest automation.\n"
    )
    for module, group in by_module(cases).items():
        path = f"tests/test_{slug(module)}.py"
        body = ["import pytest", "from playwright.sync_api import Page, expect", "", ""]
        body.append(f"class Test{pascal(module)}:")
        body.append(f'    """{module} -- {len(group)} test case(s)."""')
        for c in group:
            name = f"test_tc_{c['id']:04d}_{slug(c['title'])}"
            body += ["", f'    @pytest.mark.tc("{tc(c["id"])}")',
                     f"    def {name}(self, page: Page):",
                     comment_block(c, "#", 8),
                     f'        pytest.skip("{tc(c["id"])} not implemented yet")']
            mapping.append((c, path, f"Test{pascal(module)}::{name}"))
        files[path] = "\n".join(body) + "\n"
    return files, mapping


# ---------------------------------------------------------------------------
# Playwright JavaScript
# ---------------------------------------------------------------------------

def gen_pw_js(cases):
    files, mapping = {}, []
    files["package.json"] = (
        '{\n  "name": "cflow-playwright-tests",\n  "version": "1.0.0",\n  "private": true,\n'
        '  "scripts": { "test": "playwright test", "test:headed": "playwright test --headed" },\n'
        '  "devDependencies": { "@playwright/test": "^1.58.0" }\n}\n'
    )
    files["playwright.config.js"] = (
        "// @ts-check\nconst { defineConfig } = require('@playwright/test');\n\n"
        "module.exports = defineConfig({\n  testDir: './tests',\n"
        f"  use: {{ baseURL: '{APP_URL}', headless: false, screenshot: 'only-on-failure' }},\n"
        "  reporter: [['html'], ['junit', { outputFile: 'results.xml' }]],\n});\n"
    )
    files["README.md"] = (
        "# CFlow test cases -- Playwright JavaScript\n\n"
        "```bash\nnpm install\nnpx playwright install\nnpx playwright test\n```\n\n"
        "Each test is `test.fixme` (pending) until implemented. Test titles start with the\n"
        "TC id; `test_case_map.csv` links them back to the CFlow pytest automation.\n"
    )
    for module, group in by_module(cases).items():
        path = f"tests/{slug(module)}.spec.js"
        body = ["// @ts-check", "const { test, expect } = require('@playwright/test');", "",
                f"test.describe('{js_str(module)}', () => {{"]
        for i, c in enumerate(group):
            title = f"{tc(c['id'])} {c['title']}"
            if i:
                body.append("")
            body += [f"  test.fixme('{js_str(title)}', async ({{ page }}) => {{",
                     comment_block(c, "//", 4),
                     "  });"]
            mapping.append((c, path, title))
        body.append("});")
        files[path] = "\n".join(body) + "\n"
    return files, mapping


# ---------------------------------------------------------------------------
# Selenium Java (TestNG, Maven)
# ---------------------------------------------------------------------------

JAVA_PKG = "com.cflow.tests"


def gen_selenium_java(cases):
    files, mapping = {}, []
    pkg_dir = "src/test/java/" + JAVA_PKG.replace(".", "/")
    files["pom.xml"] = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.cflow</groupId>
  <artifactId>cflow-selenium-tests</artifactId>
  <version>1.0.0</version>
  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.seleniumhq.selenium</groupId>
      <artifactId>selenium-java</artifactId>
      <version>4.25.0</version>
    </dependency>
    <dependency>
      <groupId>org.testng</groupId>
      <artifactId>testng</artifactId>
      <version>7.10.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.5.1</version>
        <configuration>
          <suiteXmlFiles><suiteXmlFile>testng.xml</suiteXmlFile></suiteXmlFiles>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
"""
    files[f"{pkg_dir}/BaseTest.java"] = f"""package {JAVA_PKG};

import java.time.Duration;
import org.openqa.selenium.WebDriver;
import org.openqa.selenium.chrome.ChromeDriver;
import org.testng.annotations.AfterClass;
import org.testng.annotations.BeforeClass;

public abstract class BaseTest {{
    protected static final String BASE_URL = "{APP_URL}";
    protected WebDriver driver;

    @BeforeClass
    public void setUp() {{
        driver = new ChromeDriver();   // Selenium Manager resolves the driver
        driver.manage().window().maximize();
        driver.manage().timeouts().implicitlyWait(Duration.ofSeconds(10));
        driver.get(BASE_URL);
    }}

    @AfterClass(alwaysRun = true)
    public void tearDown() {{
        if (driver != null) driver.quit();
    }}
}}
"""
    classes = []
    for module, group in by_module(cases).items():
        cls = f"{pascal(module)}Test"
        classes.append(cls)
        path = f"{pkg_dir}/{cls}.java"
        body = [f"package {JAVA_PKG};", "", "import org.testng.SkipException;",
                "import org.testng.annotations.Test;", "",
                f"/** {module} -- {len(group)} test case(s). */",
                f"public class {cls} extends BaseTest {{"]
        for c in group:
            method = f"tc{c['id']:04d}_{slug(c['title'], 50)}"
            body += ["",
                     f'    @Test(description = "{java_str(tc(c["id"]) + ": " + c["title"])}", groups = {{"{java_str(c["priority"])}"}})',
                     f"    public void {method}() {{",
                     comment_block(c, "//", 8),
                     f'        throw new SkipException("{tc(c["id"])} not implemented yet");',
                     "    }"]
            mapping.append((c, path, f"{cls}#{method}"))
        body.append("}")
        files[path] = "\n".join(body) + "\n"
    files["testng.xml"] = (
        '<!DOCTYPE suite SYSTEM "https://testng.org/testng-1.0.dtd">\n'
        '<suite name="CFlow">\n  <test name="All test cases">\n    <classes>\n'
        + "".join(f'      <class name="{JAVA_PKG}.{c}"/>\n' for c in classes)
        + "    </classes>\n  </test>\n</suite>\n"
    )
    files["README.md"] = (
        "# CFlow test cases -- Selenium Java (TestNG)\n\n"
        "```bash\nmvn test\n```\n\n"
        "Each test throws `SkipException` (pending) until implemented. `test_case_map.csv`\n"
        "links every test method back to its TC id and the CFlow pytest automation.\n"
    )
    return files, mapping


GENERATORS = {"pw-py": gen_pw_py, "pw-js": gen_pw_js, "selenium-java": gen_selenium_java}


def build_zip(fmt, cases, root_name):
    """cases: dicts with id/title/module/priority/status/preconditions/steps/expected
    and 'links' (list of existing pytest node ids)."""
    files, mapping = GENERATORS[fmt](cases)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Case ID", "Title", "Module", "Status", "Generated File", "Generated Test",
                "Existing CFlow Automation (pytest)"])
    for c, path, test in mapping:
        w.writerow([tc(c["id"]), c["title"], c["module"], c["status"], path, test, "\n".join(c["links"])])
    files["test_case_map.csv"] = "\ufeff" + buf.getvalue()

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in files.items():
            z.writestr(f"{root_name}/{path}", text)
    return out.getvalue()
