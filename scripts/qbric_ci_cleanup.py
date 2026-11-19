#!/usr/bin/env python3
"""Delete this workflow run's qbric-data artifact once the run has reported (QBRIC docs/88).

The artifact holds an API test run's data (no secrets). It is kept only while the run needs it: the report job calls
this after QBRIC has the results. A run without it has nothing to delete. Never fails the job: a leftover artifact
still expires after a day.

Environment (set by GitHub Actions): GITHUB_API_URL, GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_TOKEN.
"""
import json
import os
import sys
import urllib.error
import urllib.request

NAME = "qbric-data"


def call(method, url, token):
    request = urllib.request.Request(url, method=method, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode()
        return json.loads(body) if body else None


def main(env, out=sys.stdout):
    needed = ["GITHUB_API_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_TOKEN"]
    if any(not env.get(n) for n in needed):
        out.write("::warning::QBRIC test data not deleted: the job has no GitHub token or run\n")
        return 0
    base = f"{env['GITHUB_API_URL']}/repos/{env['GITHUB_REPOSITORY']}/actions"
    try:
        listed = call("GET", f"{base}/runs/{env['GITHUB_RUN_ID']}/artifacts?name={NAME}", env["GITHUB_TOKEN"])
        found = [a for a in (listed or {}).get("artifacts", []) if a.get("name") == NAME]
        for artifact in found:
            call("DELETE", f"{base}/artifacts/{artifact['id']}", env["GITHUB_TOKEN"])
    except (urllib.error.URLError, ValueError, KeyError) as error:
        out.write(f"::warning::QBRIC test data not deleted ({error}); it expires after a day\n")
        return 0
    out.write(f"QBRIC test data deleted: {len(found)} artifact(s)\n" if found else "No QBRIC test data to delete\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
