import contextlib
import http.server
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import qbric_ci_plan as plan  # noqa: E402

PLAN = {"schemaVersion": 2, "runId": "exec-1", "rows": [
    {"position": 1, "datapointId": 7, "env": {"QBRIC_PASSWORD": "s3cret"}, "command": ["npx", "playwright", "test"]},
    {"position": 2, "datapointId": 8, "env": {"QBRIC_PASSWORD": "0ther"}, "command": ["npx", "playwright", "test"]}]}


class Qbric(http.server.BaseHTTPRequestHandler):
    """Plays GitHub's OIDC token endpoint and QBRIC's plan endpoint."""
    answer = (200, json.dumps(PLAN))
    seen = []

    def do_GET(self):
        Qbric.seen.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("/token"):
            self._send(200, json.dumps({"value": "oidc-token"}))
        elif self.headers.get("Authorization") != "Bearer oidc-token":
            self._send(401, "no token")
        else:
            self._send(*Qbric.answer)

    def _send(self, status, body):
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


class PlanFetchTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Qbric)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Qbric.answer = (200, json.dumps(PLAN))
        Qbric.seen = []
        self.folder = tempfile.TemporaryDirectory()
        self.file = pathlib.Path(self.folder.name, "qbric-plan", "qbric-plan.json")

    def tearDown(self):
        self.folder.cleanup()

    def env(self, **overrides):
        values = {"QBRIC_API_URL": self.base, "QBRIC_TENANT_ID": "42", "QBRIC_CORRELATION_ID": "qb-7c41e2d0",
                  "QBRIC_PLAN_FILE": str(self.file),
                  "ACTIONS_ID_TOKEN_REQUEST_URL": self.base + "/token?x=1",
                  "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token"}
        values.update(overrides)
        return values

    def run_main(self, env):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = plan.main(env)
        return code, out.getvalue()

    def test_the_plan_is_fetched_with_the_jobs_oidc_token_and_written_for_the_owner_only(self):
        code, out = self.run_main(self.env())

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.file.read_text()), PLAN)
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        self.assertIn(("/v1/tenants/42/ci/callbacks/qb-7c41e2d0/plan", "Bearer oidc-token"), Qbric.seen)
        self.assertIn("audience=qbric-ci", Qbric.seen[0][0])
        self.assertIn("2 row(s)", out)
        self.assertNotIn("s3cret", out)

    def test_a_refused_plan_fails_the_job_with_qbrics_answer(self):
        Qbric.answer = (410, "The run has finished; its plan is gone.")

        code, out = self.run_main(self.env())

        self.assertEqual(code, 1)
        self.assertIn("410", out)
        self.assertIn("plan is gone", out)
        self.assertFalse(self.file.exists())

    def test_the_plan_it_writes_is_one_the_thin_runner_runs(self):
        import qbric_ci_run as runner
        code, _ = self.run_main(self.env())

        self.assertEqual(code, 0)
        self.assertEqual(len(runner.load(self.file)["rows"]), 2)

    def test_a_plan_of_another_schema_fails_the_job(self):
        Qbric.answer = (200, json.dumps({"schemaVersion": 1, "rows": []}))

        code, out = self.run_main(self.env())

        self.assertEqual(code, 1)
        self.assertIn("schema 1", out)

    def test_a_job_without_id_token_permission_fails_with_what_to_do(self):
        code, out = self.run_main(self.env(ACTIONS_ID_TOKEN_REQUEST_URL=""))

        self.assertEqual(code, 1)
        self.assertIn("id-token: write", out)

    def test_missing_settings_fail_the_job(self):
        code, out = self.run_main(self.env(QBRIC_TENANT_ID=""))

        self.assertEqual(code, 1)
        self.assertIn("QBRIC_TENANT_ID", out)


if __name__ == "__main__":
    unittest.main()
