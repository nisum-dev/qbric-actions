import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import qbric_ci_callback as callback  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")


def load(relative):
    return yaml.safe_load((ROOT / relative).read_text())


def uses(document):
    found = []
    for job in document.get("jobs", {}).values():
        found += [step["uses"] for step in job.get("steps", []) if "uses" in step]
    found += [step["uses"] for step in document.get("runs", {}).get("steps", []) if "uses" in step]
    return found


@unittest.skipUnless(yaml, "PyYAML is not installed")
class ManagedWorkflowTest(unittest.TestCase):

    def test_only_the_reporting_jobs_can_prove_themselves_to_qbric(self):
        jobs = load("templates/qbric-ci-managed.yml")["jobs"]
        self.assertEqual(jobs["announce"]["permissions"], {"id-token": "write"})
        self.assertEqual(jobs["report"]["permissions"], {"id-token": "write"})
        self.assertNotIn("id-token", jobs["test"]["permissions"])

    def test_the_workflow_takes_the_inputs_qbric_dispatches_with(self):
        workflow = load("templates/qbric-ci-managed.yml")
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"correlation_id", "sha", "suite", "environment"})
        self.assertEqual(workflow["run-name"], "qbric ${{ inputs.correlation_id }}")

    def test_third_party_actions_are_pinned_to_a_commit(self):
        for relative in ["templates/qbric-ci-managed.yml", "plan/action.yml", "execute/action.yml"]:
            for action in uses(load(relative)):
                if action.startswith("nisum-dev/qbric-actions/"):
                    continue
                self.assertRegex(action, PINNED, f"{relative}: {action}")

    def test_the_plan_is_kept_one_day_and_results_are_uploaded_even_when_tests_fail(self):
        plan_steps = load("plan/action.yml")["runs"]["steps"]
        upload = next(s for s in plan_steps if "upload-artifact" in s.get("uses", ""))
        self.assertEqual(upload["with"]["retention-days"], 1)
        self.assertEqual(upload["with"]["name"], "qbric-plan")
        execute_steps = load("execute/action.yml")["runs"]["steps"]
        uploads = [s for s in execute_steps if "upload-artifact" in s.get("uses", "")]
        self.assertEqual({s["with"]["name"] for s in uploads}, {"qbric-results", "qbric-report", "qbric-logs"})
        results = next(s for s in uploads if s["with"]["name"] == "qbric-results")
        paths = results["with"]["path"].strip().splitlines()
        self.assertTrue(paths[0].endswith("/manifest.json"))
        self.assertTrue(all("/rows/" in p for p in paths[1:]), "results carry the manifest and raw outputs only")
        self.assertFalse(any("attempt-*/reports/" in p or "output.log" in p for p in paths),
                         "reports and logs go separately")
        self.assertTrue(all(s.get("if") == "always()" for s in uploads))
        report = next(s for s in uploads if s["with"]["name"] == "qbric-report")
        self.assertTrue(report["with"]["include-hidden-files"], "the Cucumber report lives under .qbric/")

    def test_the_tests_run_where_the_repository_says_and_maven_repositories_get_java(self):
        workflow = load("templates/qbric-ci-managed.yml")
        test = workflow["jobs"]["test"]
        for name, job in workflow["jobs"].items():
            self.assertEqual(job["runs-on"], "${{ vars.QBRIC_RUNS_ON || 'ubuntu-24.04' }}",
                             f"{name} must run where the tests run: a self-hosted runner may be the only way to QBRIC")
        java = next(s for s in test["steps"] if "setup-java" in s.get("uses", ""))
        self.assertEqual(java["if"], "hashFiles('pom.xml') != ''")
        self.assertEqual((java["with"]["java-version"], java["with"]["cache"]), ("17", "maven"))
        report = next(s for s in workflow["jobs"]["report"]["steps"] if s.get("with", {}).get("phase") == "completed")
        self.assertEqual(report["with"]["results-file"], "qbric-results/manifest.json")


class NoQbricBinaryTest(unittest.TestCase):
    """QBRIC's code never leaves QBRIC: nothing here is or fetches a jar or a release asset."""

    def test_the_repository_holds_no_jar(self):
        self.assertEqual([p for p in ROOT.rglob("*.jar")], [])

    def test_no_file_downloads_a_release_runs_java_or_describes_the_engine(self):
        skipped = {"tests", ".git", "__pycache__"}
        files = [p for p in ROOT.rglob("*") if p.is_file() and not skipped & set(p.relative_to(ROOT).parts)]
        self.assertTrue(any(p.name == "action.yml" for p in files))
        for path in files:
            text = path.read_text(errors="ignore")
            for forbidden in ("release download", "releases/download", "java -jar", ".jar", "run-plan",
                              "qbric-execute", "execute-cli"):
                self.assertNotIn(forbidden, text, f"{path.relative_to(ROOT)} mentions {forbidden!r}")


class ResultsSummaryTest(unittest.TestCase):

    def test_the_completed_callback_adds_up_every_datapoint_row(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            results = pathlib.Path(folder, "results.json")
            results.write_text(json.dumps({"rows": [
                {"total": 3, "passed": 3, "failed": 0, "skipped": 0},
                {"total": 3, "passed": 2, "failed": 1, "skipped": 0}]}))
            body = json.loads(callback.payload({
                "QBRIC_PHASE": "completed", "QBRIC_PROVIDER_RUN_ID": "900", "QBRIC_ATTEMPT": "1",
                "QBRIC_JOB_OUTCOME": "failure", "QBRIC_RESULTS_FILE": str(results),
                "QBRIC_JUNIT_DIR": folder}))
        self.assertEqual(body["summary"], {"total": 6, "passed": 5, "failed": 1, "skipped": 0})

    def test_without_a_results_file_the_junit_folder_is_summarised_as_before(self):
        self.assertIsNone(callback.summarise_results(None))
        self.assertIsNone(callback.summarise_results("/nonexistent/results.json"))


if __name__ == "__main__":
    unittest.main()
