"""Plan values never reach a kept output, and redacting changes nothing QBRIC counts."""
import io
import json
import pathlib
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import qbric_ci_results as results  # noqa: E402
import qbric_ci_run as runner  # noqa: E402
import qbric_ci_secrets as secrets  # noqa: E402

PASSWORD = "Hunter2!pw"
PLAN = {"rows": [{"env": {"QBRIC_PASSWORD": PASSWORD, "QBRIC_PIN": "1234", "QBRIC_SHORT": "on",
                          "QBRIC_NOTE": "first line\nsecond line", "QBRIC_EMPTY": ""}}]}


class RedactorTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.folder.name)
        self.redactor = secrets.Redactor(secrets.values(PLAN))

    def tearDown(self):
        self.folder.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_every_line_of_every_value_is_masked_and_redacted(self):
        self.assertEqual(secrets.values(PLAN), ["second line", PASSWORD, "first line", "1234", "on"])
        log = self.write("output.log", f"typed {PASSWORD}\nnote: first line\nsecond line\npin 1234 on\n")

        self.redactor.tree(self.root)

        self.assertEqual(log.read_text(), "typed ***\nnote: ***\n***\npin *** on\n",
                         "a value under four characters is left, as it is no secret")

    def test_a_json_report_keeps_its_numbers_and_shape(self):
        report = self.write(results.PLAYWRIGHT_REPORT, json.dumps({"suites": [{"file": "A.spec.ts", "specs": [
            {"title": "login", "tests": [{"status": "unexpected", "results": [
                {"duration": 1234, "error": {"message": f"expected \"{PASSWORD}\" in ünïcode"}}]}]}]}]}))

        self.redactor.tree(self.root)

        data = json.loads(report.read_text(encoding="utf-8"))
        result = data["suites"][0]["specs"][0]["tests"][0]["results"][0]
        self.assertEqual(result["duration"], 1234)
        self.assertEqual(result["error"]["message"], "expected \"***\" in ünïcode")
        self.assertEqual(results.playwright(report).counts(), {"total": 1, "passed": 0, "failed": 1, "skipped": 0})

    def test_a_surefire_report_keeps_its_counts_and_names_even_when_they_equal_a_value(self):
        report = self.write("target/surefire-reports/TEST-UserApiTest.xml",
                            '<testsuite name="UserApiTest" tests="1234" failures="1" errors="0" skipped="0">'
                            '<testcase classname="UserApiTest" name="createUser">'
                            f'<failure message="token {PASSWORD} refused">at {PASSWORD}</failure>'
                            f'</testcase><system-out>pin 1234</system-out></testsuite>')

        self.redactor.tree(self.root)

        suite = ElementTree.parse(report).getroot()
        failure = suite.find("testcase/failure")
        self.assertEqual((suite.get("tests"), failure.get("message"), failure.text), ("1234", "token *** refused",
                                                                                       "at ***"))
        self.assertEqual(suite.find("system-out").text, "pin ***")
        self.assertEqual(results.maven(report.parent).counts()["total"], 1234)

    def test_binary_files_and_links_are_left_alone(self):
        image = self.root / "shot.png"
        image.write_bytes(b"\x89PNG" + PASSWORD.encode())
        outside = tempfile.TemporaryDirectory()
        target = pathlib.Path(outside.name, "elsewhere.log")
        target.write_text(PASSWORD)
        (self.root / "link.log").symlink_to(target)

        self.redactor.tree(self.root)

        self.assertEqual(image.read_bytes(), b"\x89PNG" + PASSWORD.encode())
        self.assertEqual(target.read_text(), PASSWORD)
        outside.cleanup()

    def test_a_plan_without_values_redacts_nothing(self):
        log = self.write("output.log", "1234")

        secrets.Redactor(secrets.values({"rows": [{"env": {}}]})).tree(self.root)

        self.assertEqual(log.read_text(), "1234")


class KeptOutputsTest(unittest.TestCase):

    def test_a_row_keeps_redacted_outputs_but_decides_from_what_its_tests_wrote(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace, out = pathlib.Path(folder, "checkout"), pathlib.Path(folder, "out")
            workspace.mkdir()
            plan_row = {"position": 1, "datapointId": 7, "env": {"QBRIC_PASSWORD": PASSWORD}, "files": {},
                        "command": ["npx", "playwright", "test"], "stateDirEnv": None,
                        "outputs": [results.PLAYWRIGHT_REPORT]}

            def run(command, ws, env, timeout, log, o):
                log.write_text(f"filled {env['QBRIC_PASSWORD']}\n")
                (ws / "qbric-out").mkdir(exist_ok=True)
                (ws / results.PLAYWRIGHT_REPORT).write_text(json.dumps({"suites": [{"file": "A.spec.ts", "specs": [
                    {"title": f"uses {PASSWORD}", "tests": [{"status": "expected"}]}]}]}))
                return 0, 5, False

            outcome = runner.run_row(plan_row, {"retry": None, "timeoutMinutes": 5, "rows": [plan_row]}, workspace,
                                     out, runner.Budget(None), io.StringIO(), run=run)

            kept = out / "rows/1/attempt-1"
            self.assertTrue(outcome["green"])
            self.assertEqual((kept / "output.log").read_text(), "filled ***\n")
            self.assertNotIn(PASSWORD, (kept / results.PLAYWRIGHT_REPORT).read_text())
            self.assertIn(PASSWORD, (workspace / results.PLAYWRIGHT_REPORT).read_text(), "the checkout is untouched")


if __name__ == "__main__":
    unittest.main()
