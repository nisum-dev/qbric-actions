"""What one attempt of a QBRIC plan row produced, per spec file, and how a narrowed retry merges into it.

Read the way QBRIC's own parsers read them (PlaywrightJsReportParser, SurefireReportParser) and merged the way
RunOutcome.mergeRetry merges, because the runner decides from them whether to retry and what. QBRIC parses the
same files again on import. Standard library only.
"""
import json
import pathlib
import re
import xml.etree.ElementTree as ElementTree

PLAYWRIGHT_REPORT = "qbric-out/playwright-report.json"
SUREFIRE_REPORTS = "target/surefire-reports"
NO_FILE = ""  # tests a report does not tie to a spec file, such as every Surefire test

# Playwright reads a positional filter as a regular expression; these are escaped, as TestCommandBuilder does.
FILTER_META = re.compile(r"[\\.\[\]{}()*+?^$|]")
SPEC_FILE = re.compile(r".*\.(spec|test)\.[cm]?[jt]sx?$")
KEYS = ("total", "passed", "failed", "skipped")


class Attempt:
    """Counts by spec file ({@code NO_FILE} for tests without one)."""

    def __init__(self, by_file=None):
        self.by_file = by_file or {}

    def count(self, key):
        return sum(c[key] for c in self.by_file.values())

    def counts(self):
        return {key: self.count(key) for key in KEYS}

    def green(self):
        return self.count("failed") == 0 and self.count("total") > 0

    def failed_files(self):
        """The spec files with a failed test, or None when a failure is not tied to one (the whole row reruns)."""
        failed = {f for f, c in self.by_file.items() if c["failed"]}
        return None if not failed or NO_FILE in failed else failed

    def files(self):
        return {f for f in self.by_file if f != NO_FILE}

    def merged_with(self, retry, retried_files):
        """A retried file's results replace its earlier ones only when the retry reported that file."""
        merged = {f: dict(c) for f, c in self.by_file.items()}
        for f, c in retry.by_file.items():
            if f in retried_files or f not in merged:
                merged[f] = dict(c)
            else:
                merged[f] = {key: merged[f][key] + c[key] for key in KEYS}
        return Attempt(merged)


def _tally(by_file, file, key):
    c = by_file.setdefault(file, {key: 0 for key in KEYS})
    c[key] += 1
    c["total"] += 1


def read(workspace):
    """The attempt's results, or None when it wrote none: a compile error or a crash, which a retry cannot fix."""
    workspace = pathlib.Path(workspace)
    report = workspace / PLAYWRIGHT_REPORT
    if report.is_file():
        return playwright(report)
    surefire = workspace / SUREFIRE_REPORTS
    if surefire.is_dir() and any(surefire.glob("TEST-*.xml")):
        return maven(surefire)
    return None


def playwright(report):
    try:
        root = json.loads(report.read_text())
    except (OSError, ValueError):
        return None
    by_file = {}
    for suite in root.get("suites", []):
        _walk(suite, by_file)
    return Attempt(by_file)


def _walk(suite, by_file):
    file = suite.get("file") or NO_FILE
    for spec in suite.get("specs", []):
        for test in spec.get("tests", []):
            status = test.get("status", "")
            if _failed(status, test):
                _tally(by_file, file, "failed")
            else:
                _tally(by_file, file, "skipped" if status == "skipped" else "passed")
    for nested in suite.get("suites", []):
        _walk(nested, by_file)


def _failed(status, test):
    if status == "unexpected":
        return True
    if status in ("expected", "flaky", "skipped"):
        return False
    return any(r.get("status") in ("failed", "timedOut", "interrupted") for r in test.get("results", []))


def maven(folder):
    """Surefire results carry no spec file, so a Maven failure is never narrowed: the whole row runs again."""
    c = {key: 0 for key in KEYS}
    for xml in sorted(folder.glob("TEST-*.xml")):
        try:
            suite = ElementTree.parse(xml).getroot()
        except (OSError, ElementTree.ParseError):
            continue
        tests, skipped = int(suite.get("tests", 0)), int(suite.get("skipped", 0))
        failed = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
        c["total"] += tests
        c["failed"] += failed
        c["skipped"] += skipped
        c["passed"] += max(0, tests - failed - skipped)
    return Attempt({NO_FILE: c})


def file_filter(path):
    """A spec file or folder as Playwright's positional filter, exactly as TestCommandBuilder.asFileFilter."""
    p = path.replace("\\", "/")
    if not SPEC_FILE.match(p.lower()):
        p = p if p.endswith("/") else p + "/"
    return FILTER_META.sub(lambda m: "\\" + m.group(0), p)
