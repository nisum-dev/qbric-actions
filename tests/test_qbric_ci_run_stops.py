"""A run stopped part-way still reports what it finished; the checkout's links never lead the runner out of it."""
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import qbric_ci_paths as paths  # noqa: E402
import qbric_ci_results as results  # noqa: E402
import qbric_ci_run as runner  # noqa: E402

PW = ["npx", "playwright", "test", "--config=qbric.playwright.config.ts"]
GREEN = json.dumps({"suites": [{"file": "A.spec.ts", "specs": [{"title": "a", "tests": [{"status": "expected"}]}]}]})


def row(position, **extra):
    return dict({"position": position, "datapointId": 10 + position, "env": {"QBRIC_EMAIL": f"qa{position}@x.test"},
                 "files": {}, "command": PW, "stateDirEnv": None, "outputs": [results.PLAYWRIGHT_REPORT]}, **extra)


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class StoppedRunTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.folder.name)
        self.workspace, self.out = base / "checkout", base / "out"
        self.workspace.mkdir()

    def tearDown(self):
        self.folder.cleanup()

    def main(self, plan, run):
        path = pathlib.Path(self.folder.name, "plan.json")
        path.write_text(json.dumps(dict({"schemaVersion": 2, "runId": "exec-1", "timeoutMinutes": 30}, **plan)))
        with mock.patch.object(runner, "run_command", run):
            code = runner.main(["--plan", str(path), "--workspace", str(self.workspace), "--out", str(self.out)],
                               env={}, out=io.StringIO())
        manifest = self.out / "manifest.json"
        return code, json.loads(manifest.read_text()) if manifest.exists() else None

    def green(self, *args):
        (self.workspace / results.PLAYWRIGHT_REPORT).parent.mkdir(parents=True, exist_ok=True)
        (self.workspace / results.PLAYWRIGHT_REPORT).write_text(GREEN)
        return 0, 5, False

    def test_a_job_stopped_during_a_row_still_reports_the_rows_before_it(self):
        calls = []

        def run(*args):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt("signal 15")
            return self.green()

        code, manifest = self.main({"rows": [row(1), row(2), row(3)]}, run)

        self.assertEqual(code, runner.FAILED)
        first, second, third = manifest["rows"]
        self.assertEqual((first["green"], first["total"]), (True, 1))
        self.assertEqual((second["attempts"], third["attempts"]), ([], []))
        self.assertIn("stopped", second["stopped"])
        self.assertIn("stopped", manifest["stopped"])

    def test_the_manifest_is_on_disk_after_each_row(self):
        seen = []

        def run(*args):
            seen.append((self.out / "manifest.json").exists())
            return self.green()

        self.main({"rows": [row(1), row(2)]}, run)

        self.assertEqual(seen, [False, True])

    def test_a_row_that_cannot_be_used_keeps_the_rows_before_it(self):
        outside = pathlib.Path(self.folder.name, "outside")
        outside.mkdir()

        def run(*args):
            (self.workspace / "linked").symlink_to(outside)  # the first row's tests leave a link behind
            return self.green()

        code, manifest = self.main({"rows": [row(1), row(2, files={"linked/x.ts": "x"})]}, run)

        self.assertEqual(code, runner.UNUSABLE)
        self.assertEqual([r["total"] for r in manifest["rows"]], [1, 0])
        self.assertIn("row 2 could not be used", manifest["rows"][1]["stopped"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_file_through_a_link_out_of_the_checkout_stops_the_plan_before_anything_runs(self):
        outside = pathlib.Path(self.folder.name, "outside")
        outside.mkdir()
        (self.workspace / "linked").symlink_to(outside)
        ran = []

        code, manifest = self.main({"rows": [row(1), row(2, files={"linked/x.ts": "x"})]},
                                   lambda *a: ran.append(1) or self.green())

        self.assertEqual((code, ran, manifest), (runner.UNUSABLE, [], None))


class DeadlineTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.folder.name)
        self.workspace, self.out = base / "checkout", base / "out"
        self.workspace.mkdir()
        self.clock = Clock()

    def tearDown(self):
        self.folder.cleanup()

    def run_row(self, plan_row, script, deadline, retry=None):
        plan = {"retry": retry, "timeoutMinutes": 10, "rows": [plan_row]}
        return runner.run_row(plan_row, plan, self.workspace, self.out, runner.Budget(retry), io.StringIO(),
                              run=script, sleep=lambda s: None, deadline=deadline)

    def test_an_attempt_gets_what_is_left_of_the_runs_time_limit(self):
        deadline = runner.Deadline(10, clock=self.clock)
        self.clock.now = 4 * 60
        timeouts = []

        self.run_row(row(1), lambda c, w, e, timeout, log, o: timeouts.append(timeout) or (1, 5, False), deadline)

        self.assertEqual(timeouts, [6 * 60])

    def test_no_attempt_starts_once_the_limit_has_passed(self):
        deadline = runner.Deadline(10, clock=self.clock)
        failing = json.dumps({"suites": [{"file": "A.spec.ts", "specs": [{"title": "a",
                                                                          "tests": [{"status": "unexpected"}]}]}]})
        calls = []

        def run(*args):
            calls.append(1)
            (self.workspace / "qbric-out").mkdir(exist_ok=True)
            (self.workspace / results.PLAYWRIGHT_REPORT).write_text(failing)
            self.clock.now = 11 * 60  # the first attempt used up the run's time
            return 1, 5, True

        outcome = self.run_row(row(1), run, deadline,
                               retry={"maxAttempts": 2, "cooldownSeconds": 0, "maxRetriesPerRun": 5,
                                      "budgetSeconds": 3600})

        self.assertEqual(len(calls), 1)
        self.assertEqual(outcome["stopped"], "the run's time limit of 10 minutes was reached")
        self.assertEqual(outcome["failed"], 1, "the attempt that ran is still reported")


class CommandFailureTest(unittest.TestCase):

    def test_a_command_that_is_not_installed_is_a_failed_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            log, out = pathlib.Path(folder, "log"), io.StringIO()
            code, _, timed_out = runner.run_command(["qbric-no-such-command"], folder, dict(os.environ), 30, log, out)
            self.assertEqual((code, timed_out), (runner.NOT_FOUND, False))
            self.assertIn("could not be started", log.read_text())

    def test_output_that_is_not_utf8_is_kept_and_the_command_finishes(self):
        script = "import sys; sys.stdout.buffer.write(b'bad \\xff byte\\n' * 20000); print('done')"
        with tempfile.TemporaryDirectory() as folder:
            log = pathlib.Path(folder, "log")
            code, _, timed_out = runner.run_command([sys.executable, "-c", script], folder, dict(os.environ), 20, log,
                                                    io.StringIO())
            text = log.read_text(encoding="utf-8")
        self.assertEqual((code, timed_out), (0, False))
        self.assertIn("bad � byte", text)
        self.assertTrue(text.rstrip().endswith("done"))


class OutputLinksTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.folder.name)
        self.workspace, self.into, self.outside = base / "checkout", base / "kept", base / "outside"
        self.workspace.mkdir()
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("from outside")

    def tearDown(self):
        self.folder.cleanup()

    def test_an_output_that_is_a_link_is_not_kept(self):
        (self.workspace / "reports").symlink_to(self.outside)

        paths.keep(self.workspace, ["reports"], self.into, io.StringIO())

        self.assertFalse((self.into / "reports").exists())

    def test_an_output_under_a_linked_folder_is_not_kept(self):
        (self.workspace / "qbric-out").symlink_to(self.outside)

        paths.keep(self.workspace, ["qbric-out/secret.txt"], self.into, io.StringIO())

        self.assertFalse((self.into / "qbric-out").exists())

    def test_links_inside_a_kept_folder_are_left_out(self):
        (self.workspace / "reports").mkdir()
        (self.workspace / "reports" / "index.html").write_text("<html/>")
        (self.workspace / "reports" / "leak").symlink_to(self.outside / "secret.txt")
        (self.workspace / "reports" / "dir").symlink_to(self.outside)

        paths.keep(self.workspace, ["reports"], self.into, io.StringIO())

        self.assertEqual(sorted(p.name for p in (self.into / "reports").iterdir()), ["index.html"])

    def test_clearing_outputs_leaves_a_link_and_what_it_points_to(self):
        (self.workspace / "reports").symlink_to(self.outside)
        (self.workspace / "report.json").symlink_to(self.outside / "secret.txt")

        paths.clear(self.workspace, ["reports", "report.json"])

        self.assertTrue((self.workspace / "reports").is_symlink())
        self.assertTrue((self.workspace / "report.json").is_symlink())
        self.assertTrue((self.outside / "secret.txt").exists())


if __name__ == "__main__":
    unittest.main()
