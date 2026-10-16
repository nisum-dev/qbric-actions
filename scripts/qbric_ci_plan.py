#!/usr/bin/env python3
"""Fetch the run plan QBRIC made for this run and write it to a file, for the thin runner (qbric_ci_run.py).

Configured by environment variables, standard library only. Unlike the callback, a plan that cannot
be fetched fails the job: without it there is nothing to run. The plan holds the run's variable
values, so it is written with owner-only permissions and never printed.

Required:
  QBRIC_API_URL          base URL of qbric-api, https
  QBRIC_TENANT_ID        QBRIC tenant id
  QBRIC_CORRELATION_ID   the run's QBRIC correlation id (the workflow's correlation_id input)
  QBRIC_PLAN_FILE        where to write the plan
Authentication:
  ACTIONS_ID_TOKEN_REQUEST_URL/TOKEN  set by GitHub Actions when the job has id-token: write
"""
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

import qbric_ci_callback as callback
from qbric_ci_run import SCHEMA

REQUIRED = ["QBRIC_API_URL", "QBRIC_TENANT_ID", "QBRIC_CORRELATION_ID", "QBRIC_PLAN_FILE"]


def plan_url(env):
    return callback.endpoint(env) + "/plan"


def fetch(url, token, attempts=3):
    """The plan's JSON text; raises RuntimeError with QBRIC's answer when it is refused."""
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                                       "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode()
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == attempts:
                raise RuntimeError(f"QBRIC answered {error.code}: {error.read().decode()[:300]}") from error
        except urllib.error.URLError as error:
            if attempt == attempts:
                raise RuntimeError(f"QBRIC could not be reached: {error.reason}") from error
        time.sleep(2 * attempt)
    raise RuntimeError("the plan was not fetched")


def write_plan(path, text):
    plan = json.loads(text)
    if plan.get("schemaVersion") != SCHEMA:
        raise RuntimeError(f"QBRIC sent plan schema {plan.get('schemaVersion')}; this action reads schema {SCHEMA}")
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    return len(plan.get("rows", []))


def main(env):
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        print("::error::QBRIC plan not fetched, missing " + ", ".join(missing))
        return 1
    if not (env.get("ACTIONS_ID_TOKEN_REQUEST_URL") and env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")):
        print("::error::QBRIC plan not fetched: give the job id-token: write")
        return 1
    try:
        rows = write_plan(env["QBRIC_PLAN_FILE"], fetch(plan_url(env), callback.github_token(env)))
    except (ValueError, OSError, RuntimeError) as error:
        print(f"::error::QBRIC plan not fetched: {error}")
        return 1
    print(f"QBRIC plan fetched: {rows} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
