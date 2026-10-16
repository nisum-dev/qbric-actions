import hashlib
import hmac
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import qbric_ci_callback as callback  # noqa: E402

JUNIT = """<testsuites><testsuite name="todo.spec.ts">
<testcase name="a"/><testcase name="b"><failure message="x"/></testcase>
<testcase name="c"><skipped/></testcase><testcase name="d"><error message="y"/></testcase>
</testsuite></testsuites>"""


def env(**overrides):
    base = {"QBRIC_API_URL": "https://api.qbric.example", "QBRIC_TENANT_ID": "42",
            "QBRIC_CORRELATION_ID": "qb-7c41e2d0", "QBRIC_PHASE": "completed",
            "QBRIC_PROVIDER_RUN_ID": "900", "QBRIC_ATTEMPT": "2", "QBRIC_JOB_OUTCOME": "failure",
            "QBRIC_CHECKED_OUT_SHA": "9f3c2a1e4b7d0c6a5f8e2d1b3c4a5e6f7a8b9c0d"}
    base.update(overrides)
    return base


class PayloadTest(unittest.TestCase):

    def test_completed_payload_matches_the_schema_qbric_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            pathlib.Path(folder, "junit.xml").write_text(JUNIT)
            body = json.loads(callback.payload(env(QBRIC_JUNIT_DIR=folder)))
        self.assertEqual(body["schemaVersion"], 2)
        self.assertEqual(body["phase"], "COMPLETED")
        self.assertEqual(body["providerRunId"], "900")
        self.assertEqual(body["attempt"], 2)
        self.assertEqual(body["jobOutcome"], "failure")
        self.assertEqual(body["summary"], {"total": 4, "passed": 1, "failed": 2, "skipped": 1})

    def test_running_payload_carries_no_summary(self):
        body = json.loads(callback.payload(env(QBRIC_PHASE="running")))
        self.assertEqual(body["phase"], "RUNNING")
        self.assertNotIn("summary", body)

    def test_no_junit_files_means_no_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(json.loads(callback.payload(env(QBRIC_JUNIT_DIR=folder)))["summary"])

    def test_unknown_phase_is_refused(self):
        with self.assertRaises(ValueError):
            callback.payload(env(QBRIC_PHASE="finished"))


class EndpointTest(unittest.TestCase):

    def test_ids_are_escaped_into_the_path(self):
        url = callback.endpoint(env(QBRIC_CORRELATION_ID="qb-1/../admin"))
        self.assertEqual(url, "https://api.qbric.example/v1/tenants/42/ci/callbacks/qb-1%2F..%2Fadmin")

    def test_plain_http_is_refused_outside_localhost(self):
        with self.assertRaises(ValueError):
            callback.endpoint(env(QBRIC_API_URL="http://api.qbric.example"))


class AuthTest(unittest.TestCase):

    def test_hmac_signs_timestamp_and_body(self):
        headers = callback.auth_headers(env(QBRIC_CALLBACK_SECRET="s3cret"), "{}")
        expected = hmac.new(b"s3cret", (headers["X-QBRIC-Timestamp"] + ".{}").encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-QBRIC-Signature"], "sha256=" + expected)

    def test_without_credentials_nothing_is_sent(self):
        with self.assertRaises(ValueError):
            callback.auth_headers(env(), "{}")


class MainTest(unittest.TestCase):

    def test_missing_configuration_never_fails_the_build(self):
        self.assertEqual(callback.main({"QBRIC_PHASE": "running"}), 0)

    def test_missing_credentials_never_fail_the_build(self):
        self.assertEqual(callback.main(env()), 0)


if __name__ == "__main__":
    unittest.main()
