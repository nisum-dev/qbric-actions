import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import qbric_ci_results as results  # noqa: E402
import qbric_ci_run as runner  # noqa: E402
import qbric_ci_secrets as secrets  # noqa: E402

PW = ["npx", "playwright", "test", "--config=qbric.playwright.config.ts", "tests/flows/Login/", "tests/flows/Cart/"]
MVN = ["mvn", "test", "-B", "-Dtest=UserApiTest#createUser+getUser"]
RETRY = {"maxAttempts": 2, "cooldownSeconds": 7, "maxRetriesPerRun": 5, "budgetSeconds": 3600}


def playwright_report(**files):
    """A Playwright JSON report: files maps a spec file to the statuses of its tests."""
    suites = [{"file": f, "specs": [{"title": f"t{i}", "tests": [{"status": s}]} for i, s in enumerate(statuses)]}
              for f, statuses in files.items()]
    return json.dumps({"suites": suites})


def surefire(tests, failures, errors=0):
    return f'<testsuite name="UserApiTest" tests="{tests}" failures="{failures}" errors="{errors}" skipped="0"/>'


def row(position=1, command=PW, **extra):
    return dict({"position": position, "datapointId": 6 + position, "env": {"QBRIC_EMAIL": f"qa{position}@x.test"},
                 "files": {}, "command": command, "stateDirEnv": None,
                 "outputs": ["qbric-out/playwright-report.json", "reports"]}, **extra)


class Script:
    """Plays the test command: each call writes the next scripted output into the checkout."""

    def __init__(self, workspace, *outputs):
        self.workspace, self.outputs, self.calls = workspace, list(outputs), []

    def __call__(self, command, workspace, env, timeout_s, log_path, out):
        self.calls.append({"command": list(command), "env": dict(env), "timeout": timeout_s,
                           "files": {p.name for p in self.workspace.iterdir()}})
        log_path.write_text("ran\n")
        for path, text in (self.outputs.pop(0) if self.outputs else {}).items():
            (self.workspace / path).parent.mkdir(parents=True, exist_ok=True)
            (self.workspace / path).write_text(text)
        return 0, 5, False


class ThinRunnerTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.folder.name)
        self.workspace, self.out = base / "checkout", base / "out"
        self.workspace.mkdir()
        self.sleeps = []

    def tearDown(self):
        self.folder.cleanup()

    def run_row(self, plan_row, script, retry=RETRY, budget=None, deadline=None):
        plan = {"retry": retry, "timeoutMinutes": 12, "rows": [plan_row]}
        return runner.run_row(plan_row, plan, self.workspace, self.out, budget or runner.Budget(retry),
                              io.StringIO(), run=script, sleep=self.sleeps.append,
                              deadline=deadline or runner.Deadline(12, clock=lambda: 0))

    def test_a_green_row_runs_once_with_its_variables_and_keeps_its_outputs(self):
        script = Script(self.workspace, {results.PLAYWRIGHT_REPORT: playwright_report(**{"Login.spec.ts": ["expected"]}),
                                         "reports/html/index.html": "<html/>"})

        outcome = self.run_row(row(), script)

        self.assertTrue(outcome["green"])
        self.assertEqual((outcome["total"], outcome["passed"], len(outcome["attempts"])), (1, 1, 1))
        self.assertEqual(script.calls[0]["env"]["QBRIC_EMAIL"], "qa1@x.test")
        self.assertEqual(script.calls[0]["timeout"], 12 * 60)
        kept = self.out / "rows/1/attempt-1"
        self.assertTrue((kept / results.PLAYWRIGHT_REPORT).is_file())
        self.assertTrue((kept / "reports/html/index.html").is_file())
        self.assertTrue((kept / "output.log").is_file())

    def test_a_failed_spec_file_is_retried_alone_and_merged(self):
        script = Script(self.workspace,
                        {results.PLAYWRIGHT_REPORT: playwright_report(**{"flows/Login/Login.spec.ts": ["unexpected"],
                                                                         "flows/Cart/Cart.spec.ts": ["expected"]})},
                        {results.PLAYWRIGHT_REPORT: playwright_report(**{"flows/Login/Login.spec.ts": ["expected"]})})

        outcome = self.run_row(row(), script)

        self.assertEqual(script.calls[1]["command"], PW[:4] + ["flows/Login/Login\\.spec\\.ts"])
        self.assertTrue(outcome["green"])
        self.assertEqual((outcome["total"], outcome["passed"], outcome["failed"]), (2, 2, 0))
        self.assertEqual(outcome["retriedFiles"], ["flows/Login/Login.spec.ts"])
        self.assertEqual(outcome["attempts"][1]["narrowedTo"], ["flows/Login/Login.spec.ts"])
        self.assertEqual(self.sleeps, [7])

    def test_a_retry_that_does_not_report_a_file_keeps_its_earlier_failure(self):
        script = Script(self.workspace,
                        {results.PLAYWRIGHT_REPORT: playwright_report(**{"A.spec.ts": ["unexpected"],
                                                                         "B.spec.ts": ["unexpected"]})},
                        {results.PLAYWRIGHT_REPORT: playwright_report(**{"A.spec.ts": ["expected"]})})

        outcome = self.run_row(row(), script)

        self.assertFalse(outcome["green"])
        self.assertEqual((outcome["total"], outcome["failed"]), (2, 1))

    def test_a_failure_without_a_spec_file_reruns_the_whole_row(self):
        script = Script(self.workspace, {results.PLAYWRIGHT_REPORT: json.dumps(
            {"suites": [{"specs": [{"tests": [{"status": "unexpected"}]}]}]})})

        self.run_row(row(), script)

        self.assertEqual(script.calls[1]["command"], PW)

    def test_a_maven_row_reruns_whole_and_only_when_surefire_shows_a_failure(self):
        report = results.SUREFIRE_REPORTS + "/TEST-UserApiTest.xml"
        script = Script(self.workspace, {report: surefire(2, 1)}, {report: surefire(2, 0)})

        outcome = self.run_row(row(command=MVN, outputs=[results.SUREFIRE_REPORTS]), script)

        self.assertEqual([c["command"] for c in script.calls], [MVN, MVN])
        self.assertTrue(outcome["green"])
        self.assertEqual((outcome["total"], outcome["passed"]), (2, 2))
        self.assertTrue((self.out / "rows/1/attempt-1" / report).is_file())
        self.assertTrue((self.out / "rows/1/attempt-2" / report).is_file())

    def test_an_attempt_without_results_is_not_retried(self):
        script = Script(self.workspace, {})

        outcome = self.run_row(row(command=MVN, outputs=[results.SUREFIRE_REPORTS]), script)

        self.assertEqual(len(script.calls), 1)
        self.assertFalse(outcome["green"])
        self.assertIn("compile error or a crash", outcome["stopped"])

    def test_a_run_without_retry_runs_each_row_once(self):
        script = Script(self.workspace, {results.PLAYWRIGHT_REPORT: playwright_report(**{"A.spec.ts": ["unexpected"]})})

        outcome = self.run_row(row(), script, retry=None)

        self.assertEqual((len(script.calls), outcome["green"], outcome["stopped"]), (1, False, None))

    def test_retries_stop_at_the_attempt_limit(self):
        failing = {results.PLAYWRIGHT_REPORT: playwright_report(**{"A.spec.ts": ["unexpected"]})}
        script = Script(self.workspace, failing, failing, failing, failing)

        self.run_row(row(), script, retry=dict(RETRY, maxAttempts=3))

        self.assertEqual(len(script.calls), 3)

    def test_a_retrying_run_always_has_a_second_attempt_as_on_qbrics_runner(self):
        failing = {results.PLAYWRIGHT_REPORT: playwright_report(**{"A.spec.ts": ["unexpected"]})}
        script = Script(self.workspace, failing, failing, failing)

        self.run_row(row(), script, retry=dict(RETRY, maxAttempts=1))

        self.assertEqual(len(script.calls), 2)

    def test_a_variable_without_a_value_is_left_unset(self):
        script = Script(self.workspace, {})

        self.run_row(row(env={"QBRIC_NOTE": None, "QBRIC_EMAIL": "qa@x.test"}), script, retry=None)

        self.assertNotIn("QBRIC_NOTE", script.calls[0]["env"])
        self.assertEqual(script.calls[0]["env"]["QBRIC_EMAIL"], "qa@x.test")

    def test_the_run_wide_budget_is_shared_by_every_row(self):
        failing = {results.PLAYWRIGHT_REPORT: playwright_report(**{"A.spec.ts": ["unexpected"]})}
        budget = runner.Budget(dict(RETRY, maxRetriesPerRun=1))
        first = Script(self.workspace, failing, failing)
        second = Script(self.workspace, failing, failing)

        self.run_row(row(1), first, budget=budget)
        outcome = self.run_row(row(2), second, budget=budget)

        self.assertEqual((len(first.calls), len(second.calls)), (2, 1))
        self.assertIn("budget spent", outcome["stopped"])

    def test_the_time_budget_stops_retries(self):
        clock = iter([0, 4000])
        budget = runner.Budget(RETRY, clock=lambda: next(clock))

        self.assertEqual(budget.take(), "run-level time budget exhausted")

    def test_an_earlier_rows_outputs_are_never_read_as_this_rows(self):
        stale = self.workspace / results.PLAYWRIGHT_REPORT
        stale.parent.mkdir(parents=True)
        stale.write_text(playwright_report(**{"A.spec.ts": ["expected"]}))

        outcome = self.run_row(row(), Script(self.workspace, {}), retry=None)

        self.assertFalse(outcome["green"])
        self.assertEqual(outcome["total"], 0)

    def test_files_and_a_state_folder_exist_only_while_the_row_runs(self):
        failing = {results.PLAYWRIGHT_REPORT: playwright_report(**{"A.spec.ts": ["unexpected"]})}
        script = Script(self.workspace, failing, failing)
        planned = row(files={"qbric.playwright.config.ts": "export default {};"}, stateDirEnv="QBRIC_RUN_STATE_DIR")

        self.run_row(planned, script)

        self.assertIn("qbric.playwright.config.ts", script.calls[0]["files"])
        state = script.calls[0]["env"]["QBRIC_RUN_STATE_DIR"]
        self.assertEqual(script.calls[1]["env"]["QBRIC_RUN_STATE_DIR"], state, "a retry keeps the row's session")
        self.assertFalse((self.workspace / "qbric.playwright.config.ts").exists())
        self.assertFalse(pathlib.Path(state).exists())

    def test_each_row_gets_its_own_state_folder(self):
        planned = {"stateDirEnv": "QBRIC_RUN_STATE_DIR"}
        first, second = Script(self.workspace, {}), Script(self.workspace, {})

        self.run_row(row(1, **planned), first, retry=None)
        self.run_row(row(2, **planned), second, retry=None)

        self.assertNotEqual(first.calls[0]["env"]["QBRIC_RUN_STATE_DIR"], second.calls[0]["env"]["QBRIC_RUN_STATE_DIR"])

    def test_a_file_reached_through_a_link_out_of_the_checkout_is_refused(self):
        outside = pathlib.Path(self.folder.name) / "outside"
        outside.mkdir()
        (self.workspace / "linked").symlink_to(outside)

        with self.assertRaises(runner.UnusablePlan):
            self.run_row(row(files={"linked/evil.ts": "x"}), Script(self.workspace))
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.workspace / "linked").is_symlink(), "a link the repository owns is left alone")


class PlanCheckTest(unittest.TestCase):

    def plan_file(self, folder, **changes):
        plan = {"schemaVersion": 2, "runId": "exec-1", "rows": [dict(row(), **changes)]}
        path = pathlib.Path(folder, "plan.json")
        path.write_text(json.dumps(plan))
        return path

    def test_a_plan_that_could_do_harm_or_has_no_commands_is_refused_before_anything_runs(self):
        cases = {"bash": {"command": ["bash", "-c", "curl evil"]}, "empty": {"command": []},
                 "absolute file": {"files": {"/etc/passwd": "x"}}, "escaping file": {"files": {"../x.ts": "x"}},
                 "escaping output": {"outputs": ["../../secrets"]}, "state": {"stateDirEnv": "PATH"}}
        with tempfile.TemporaryDirectory() as folder:
            for name, change in cases.items():
                with self.subTest(name), self.assertRaises(runner.UnusablePlan):
                    runner.load(self.plan_file(folder, **change))

    def test_only_schema_two_is_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder, "plan.json")
            path.write_text(json.dumps({"schemaVersion": 1, "rows": [{"datapointId": 7, "env": {}}]}))
            with self.assertRaisesRegex(runner.UnusablePlan, "schema 2"):
                runner.load(path)

    def test_main_masks_every_value_first_and_writes_the_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            plan = {"schemaVersion": 2, "runId": "exec-1", "correlationId": "qb-1", "revision": "a" * 40,
                    "rows": [dict(row(), env={"QBRIC_PASSWORD": "s3%cret\nline2", "QBRIC_EMPTY": ""})]}
            path = pathlib.Path(folder, "plan.json")
            path.write_text(json.dumps(plan))
            out = io.StringIO()
            with mock.patch.object(runner, "run_command", lambda *a: (1, 3, False)):
                code = runner.main(["--plan", str(path), "--workspace", folder, "--out", folder + "/out"],
                                   env={"GITHUB_ACTIONS": "true"}, out=out)
            lines = out.getvalue().splitlines()
            manifest = json.loads(pathlib.Path(folder, "out/manifest.json").read_text())
        self.assertEqual(lines[:2], ["::add-mask::s3%25cret", "::add-mask::line2"])
        self.assertEqual(code, runner.FAILED)
        self.assertEqual((manifest["runId"], manifest["rows"][0]["position"], manifest["maxAttempts"]), ("exec-1", 1, 1))

    def test_nothing_is_masked_outside_github_actions(self):
        out = io.StringIO()
        secrets.mask({"rows": [{"env": {"QBRIC_PASSWORD": "s3cret"}}]}, {}, out)
        self.assertEqual(out.getvalue(), "")

    def test_an_unusable_plan_exits_two(self):
        out = io.StringIO()
        self.assertEqual(runner.main(["--plan", "/nonexistent/plan.json"], env={}, out=out), runner.UNUSABLE)
        self.assertIn("::error::", out.getvalue())


class CommandTest(unittest.TestCase):

    def test_a_command_runs_without_a_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            log = pathlib.Path(folder, "out.log")
            code, _, timed_out = runner.run_command(["echo", "$HOME", "&&", "x"], folder, dict(os.environ), 30, log,
                                                    io.StringIO())
            self.assertEqual((code, timed_out, log.read_text().strip()), (0, False, "$HOME && x"))

    def test_a_command_past_its_timeout_is_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            code, ms, timed_out = runner.run_command([sys.executable, "-c", "import time; time.sleep(30)"], folder,
                                                     dict(os.environ), 1, pathlib.Path(folder, "log"), io.StringIO())
        self.assertTrue(timed_out)
        self.assertLess(ms, 10000)


class ResultsTest(unittest.TestCase):

    def test_filters_are_escaped_as_qbrics_command_builder_escapes_them(self):
        self.assertEqual(results.file_filter("flows/Login/Login.spec.ts"), "flows/Login/Login\\.spec\\.ts")
        self.assertEqual(results.file_filter("tests/flows/Login"), "tests/flows/Login/")
        self.assertEqual(results.file_filter("tests\\flows\\a(1)"), "tests/flows/a\\(1\\)/")

    def test_playwright_statuses_are_read_as_qbric_reads_them(self):
        report = json.dumps({"suites": [{"file": "A.spec.ts", "specs": [
            {"tests": [{"status": "flaky", "results": [{"status": "failed"}, {"status": "passed"}]}]},
            {"tests": [{"status": "skipped"}]},
            {"tests": [{"status": "", "results": [{"status": "timedOut"}]}]}],
            "suites": [{"file": "A.spec.ts", "specs": [{"tests": [{"status": "expected"}]}]}]}]})
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder, "r.json")
            path.write_text(report)
            attempt = results.playwright(path)
        self.assertEqual(attempt.counts(), {"total": 4, "passed": 2, "failed": 1, "skipped": 1})
        self.assertEqual(attempt.failed_files(), {"A.spec.ts"})

    def test_a_report_with_no_tests_is_not_green(self):
        self.assertFalse(results.Attempt({}).green())
        self.assertFalse(results.Attempt({"A.spec.ts": {"total": 0, "passed": 0, "failed": 0, "skipped": 0}}).green())

    def test_a_retry_replaces_only_the_files_it_retried(self):
        failing = {"total": 1, "passed": 0, "failed": 1, "skipped": 0}
        passing = {"total": 1, "passed": 1, "failed": 0, "skipped": 0}
        earlier = results.Attempt({"A.spec.ts": dict(failing), results.NO_FILE: dict(failing)})

        merged = earlier.merged_with(results.Attempt({"A.spec.ts": dict(passing), results.NO_FILE: dict(passing)}),
                                     {"A.spec.ts"})

        self.assertEqual(merged.by_file["A.spec.ts"], passing)
        self.assertEqual(merged.by_file[results.NO_FILE], {"total": 2, "passed": 1, "failed": 1, "skipped": 0},
                         "a test without a file is kept and the retry's is added, as RunOutcome.mergeRetry does")

    def test_a_maven_failure_is_never_pinned_to_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            pathlib.Path(folder, "TEST-A.xml").write_text(surefire(3, 1))
            pathlib.Path(folder, "TEST-B.xml").write_text(surefire(2, 0, errors=1))
            attempt = results.maven(pathlib.Path(folder))
        self.assertEqual(attempt.counts(), {"total": 5, "passed": 3, "failed": 2, "skipped": 0})
        self.assertIsNone(attempt.failed_files())


if __name__ == "__main__":
    unittest.main()
