import http.server
import io
import json
import pathlib
import sys
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import qbric_ci_cleanup as cleanup  # noqa: E402


class GitHub(http.server.BaseHTTPRequestHandler):
    """Plays GitHub's artifact endpoints for one run."""
    artifacts = []
    deleted = []

    def do_GET(self):
        body = json.dumps({"artifacts": GitHub.artifacts}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        GitHub.deleted.append((self.path, self.headers.get("Authorization")))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


class CleanupTest(unittest.TestCase):
    """The report job deletes the run's test data once the run has reported (QBRIC docs/88)."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), GitHub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.env = {"GITHUB_API_URL": f"http://127.0.0.1:{cls.server.server_address[1]}",
                   "GITHUB_REPOSITORY": "acme/tests", "GITHUB_RUN_ID": "99", "GITHUB_TOKEN": "gh-token"}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        GitHub.deleted = []

    def test_the_test_data_is_deleted_and_nothing_else(self):
        GitHub.artifacts = [{"id": 5, "name": "qbric-data"}, {"id": 6, "name": "qbric-results"}]
        out = io.StringIO()

        self.assertEqual(cleanup.main(self.env, out), 0)

        self.assertEqual(GitHub.deleted, [("/repos/acme/tests/actions/artifacts/5", "Bearer gh-token")])
        self.assertIn("deleted: 1", out.getvalue())

    def test_a_run_without_test_data_has_nothing_to_delete(self):
        GitHub.artifacts = []
        out = io.StringIO()

        self.assertEqual(cleanup.main(self.env, out), 0)

        self.assertEqual(GitHub.deleted, [])
        self.assertIn("No QBRIC test data", out.getvalue())

    def test_it_never_fails_the_job(self):
        out = io.StringIO()
        self.assertEqual(cleanup.main(dict(self.env, GITHUB_API_URL="http://127.0.0.1:9"), out), 0)
        self.assertIn("expires after a day", out.getvalue())


if __name__ == "__main__":
    unittest.main()
