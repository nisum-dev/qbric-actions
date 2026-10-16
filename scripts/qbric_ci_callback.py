#!/usr/bin/env python3
"""Report a CI run's progress or result to QBRIC (callback schema version 2).

Configured entirely by environment variables, so the same script serves GitHub Actions and
Bamboo. Standard library only. A failed report prints a warning and exits 0: reporting never
fails the build, and QBRIC's poller completes the run without it.

Required:
  QBRIC_API_URL          base URL of qbric-api, https
  QBRIC_TENANT_ID        QBRIC tenant id
  QBRIC_CORRELATION_ID   the run's QBRIC correlation id (the workflow's correlation_id input)
  QBRIC_PHASE            running | completed
  QBRIC_PROVIDER_RUN_ID  the CI system's run id
  QBRIC_ATTEMPT          the run attempt, from 1
Optional:
  QBRIC_CHECKED_OUT_SHA  commit the tests ran on (git rev-parse HEAD)
  QBRIC_JOB_OUTCOME      completed only: success | failure | cancelled
  QBRIC_JUNIT_DIR        completed only: folder of JUnit XML files to summarise
  QBRIC_RESULTS_FILE     completed only: manifest.json written by the QBRIC thin runner; used instead of
                         QBRIC_JUNIT_DIR when it exists
Authentication, one of:
  ACTIONS_ID_TOKEN_REQUEST_URL/TOKEN  set by GitHub Actions when the job has id-token: write
  QBRIC_CALLBACK_SECRET               HMAC secret, for CI systems without OIDC
"""
import hashlib
import hmac
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree

AUDIENCE = "qbric-ci"
REQUIRED = ["QBRIC_API_URL", "QBRIC_TENANT_ID", "QBRIC_CORRELATION_ID", "QBRIC_PHASE",
            "QBRIC_PROVIDER_RUN_ID", "QBRIC_ATTEMPT"]


def summarise(junit_dir):
    """Counts from every JUnit XML file under junit_dir, or None when there are none."""
    files = sorted(pathlib.Path(junit_dir).rglob("*.xml")) if junit_dir else []
    if not files:
        return None
    total = passed = failed = skipped = 0
    for file in files:
        for case in ElementTree.parse(file).getroot().iter("testcase"):
            total += 1
            if case.find("failure") is not None or case.find("error") is not None:
                failed += 1
            elif case.find("skipped") is not None:
                skipped += 1
            else:
                passed += 1
    return {"total": total, "passed": passed, "failed": failed, "skipped": skipped}


def summarise_results(results_file):
    """Counts from the thin runner's manifest.json, every datapoint row added up, or None without one."""
    path = pathlib.Path(results_file) if results_file else None
    if path is None or not path.is_file():
        return None
    rows = json.loads(path.read_text()).get("rows", [])
    return {key: sum(int(row.get(key, 0)) for row in rows) for key in ("total", "passed", "failed", "skipped")}


def payload(env):
    phase = env["QBRIC_PHASE"].strip().upper()
    if phase not in ("RUNNING", "COMPLETED"):
        raise ValueError("QBRIC_PHASE must be running or completed")
    body = {
        "schemaVersion": 2,
        "phase": phase,
        "providerRunId": env["QBRIC_PROVIDER_RUN_ID"],
        "attempt": int(env["QBRIC_ATTEMPT"]),
        "checkedOutSha": env.get("QBRIC_CHECKED_OUT_SHA") or None,
        "occurredAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if phase == "COMPLETED":
        body["jobOutcome"] = env.get("QBRIC_JOB_OUTCOME", "")
        body["summary"] = (summarise_results(env.get("QBRIC_RESULTS_FILE"))
                           or summarise(env.get("QBRIC_JUNIT_DIR")))
    return json.dumps(body, separators=(",", ":"))


def github_token(env):
    url = env["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + urllib.parse.quote(AUDIENCE)
    request = urllib.request.Request(url, headers={"Authorization": "bearer " + env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["value"]


def auth_headers(env, body):
    if env.get("ACTIONS_ID_TOKEN_REQUEST_URL") and env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        return {"Authorization": "Bearer " + github_token(env)}
    secret = env.get("QBRIC_CALLBACK_SECRET")
    if secret:
        timestamp = str(int(time.time()))
        digest = hmac.new(secret.encode(), (timestamp + "." + body).encode(), hashlib.sha256).hexdigest()
        return {"X-QBRIC-Timestamp": timestamp, "X-QBRIC-Signature": "sha256=" + digest}
    raise ValueError("No credentials: give the job id-token: write, or set QBRIC_CALLBACK_SECRET")


def endpoint(env):
    base = env["QBRIC_API_URL"].rstrip("/")
    if not base.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise ValueError("QBRIC_API_URL must use https")
    tenant = urllib.parse.quote(env["QBRIC_TENANT_ID"], safe="")
    correlation = urllib.parse.quote(env["QBRIC_CORRELATION_ID"], safe="")
    return f"{base}/v1/tenants/{tenant}/ci/callbacks/{correlation}"


def send(url, body, headers, attempts=3):
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body.encode(), method="POST",
                                         headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == attempts:
                return error.code, error.read().decode()
        except urllib.error.URLError as error:
            if attempt == attempts:
                return 0, str(error.reason)
        time.sleep(2 * attempt)
    return 0, "not sent"


def main(env):
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        print("::warning::QBRIC callback skipped, missing " + ", ".join(missing))
        return 0
    try:
        body = payload(env)
        status, answer = send(endpoint(env), body, auth_headers(env, body))
    except (ValueError, OSError) as error:
        print(f"::warning::QBRIC callback not sent: {error}")
        return 0
    if 200 <= status < 300:
        print(f"QBRIC callback {env['QBRIC_PHASE']} accepted: {answer}")
    else:
        print(f"::warning::QBRIC callback answered {status}: {answer[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
