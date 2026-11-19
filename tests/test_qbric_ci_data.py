import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import qbric_ci_data as data  # noqa: E402
import qbric_ci_run as runner  # noqa: E402

ROW = {"position": 1, "env": {"QBRIC_AUTH_MECHANISM": "BEARER"}, "command": ["mvn", "test"]}


class DataAndSecretsTest(unittest.TestCase):
    """API tests on GitHub Actions (QBRIC docs/88): their test data folder and their test users' secrets."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.temp = pathlib.Path(self.folder.name)
        self.bundle = self.temp / "qbric-data.json"
        self.bundle.write_text(json.dumps({"files": {"manifest.json": "{}", "cases/GET_x.json": "[1]"}}))

    def tearDown(self):
        self.folder.cleanup()

    def test_rows_get_the_data_folder_and_only_the_secrets_the_plan_names(self):
        plan = {"rows": [ROW], "data": True, "secrets": ["QBRIC_AUTH_FULLACCESS_TOKEN"]}
        env = {data.DATA_FILE_ENV: str(self.bundle),
               data.SECRETS_ENV: json.dumps({"QBRIC_AUTH_FULLACCESS_TOKEN": "sk_test", "OTHER_SECRET": "x"})}

        ready = data.prepared(plan, env, self.temp)

        row_env = ready["rows"][0]["env"]
        self.assertEqual(row_env["QBRIC_AUTH_FULLACCESS_TOKEN"], "sk_test")
        self.assertEqual(row_env["QBRIC_AUTH_MECHANISM"], "BEARER")
        self.assertNotIn("OTHER_SECRET", row_env)
        folder = pathlib.Path(row_env["QBRIC_DATA_DIR"])
        self.assertEqual((folder / "cases" / "GET_x.json").read_text(), "[1]")
        self.assertEqual(oct(os.stat(folder / "manifest.json").st_mode & 0o777), "0o600")

    def test_a_secret_the_environment_lacks_stops_the_run_by_name(self):
        plan = {"rows": [ROW], "secrets": ["QBRIC_AUTH_FULLACCESS_TOKEN"]}

        with self.assertRaises(data.Missing) as caught:
            data.prepared(plan, {data.SECRETS_ENV: "{}"}, self.temp)
        self.assertIn("QBRIC_AUTH_FULLACCESS_TOKEN", str(caught.exception))

    def test_planned_data_that_never_arrived_stops_the_run(self):
        with self.assertRaises(data.Missing):
            data.prepared({"rows": [ROW], "data": True}, {data.DATA_FILE_ENV: str(self.temp / "none.json")}, self.temp)

    def test_a_file_outside_the_data_folder_is_refused(self):
        self.bundle.write_text(json.dumps({"files": {"../escape.json": "x"}}))
        with self.assertRaises(data.Missing):
            data.write_bundle(self.bundle, self.temp / "d")

    def test_a_plan_without_data_or_secrets_is_left_as_it_is(self):
        plan = {"rows": [ROW]}
        self.assertIs(data.prepared(plan, {}, self.temp), plan)

    def test_the_tests_never_see_the_workflows_secrets_json(self):
        seen = {}

        def run(command, workspace, env, timeout, log, out):
            seen.update(env)
            return 0, 1, False

        os.environ[data.SECRETS_ENV] = json.dumps({"ANY": "secret"})
        try:
            runner.attempts_of(dict(ROW, outputs=[]), {"rows": [ROW]}, self.temp, self.temp, runner.Budget(None),
                               io.StringIO(), run, lambda s: None, None, runner.Deadline(60))
        finally:
            del os.environ[data.SECRETS_ENV]
        self.assertNotIn(data.SECRETS_ENV, seen)
        self.assertEqual(seen["QBRIC_AUTH_MECHANISM"], "BEARER")


if __name__ == "__main__":
    unittest.main()
