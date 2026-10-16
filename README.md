# qbric-actions

GitHub Actions that let a test repository QBRIC manages run its QBRIC tests on GitHub Actions, with the
same results as QBRIC's own runner: the cases a run covers, a run per datapoint with its variables, suite
order and a shared login, and retry-until-green.

QBRIC decides every run on its own servers and sends the runner a plan. These actions only carry the plan
out and report back; they contain no QBRIC code and download nothing.

| Action | What it does |
| --- | --- |
| [`ci-callback`](ci-callback/README.md) | Tells QBRIC a run is running or finished |
| [`plan`](plan/README.md) | Fetches the run's plan from QBRIC and hands it to the test job |
| [`execute`](execute/README.md) | Runs the plan in the checkout with the thin runner and uploads the results |

[`templates/qbric-ci-managed.yml`](templates/qbric-ci-managed.yml) is the workflow that wires them
together. QBRIC commits it as `.github/workflows/qbric-ci.yml` when you press **Set up CI** in Settings →
CI/CD, together with the repository variables `QBRIC_API_URL` and `QBRIC_TENANT_ID`.

## Security

- **No stored secret for QBRIC.** The reporting jobs prove who they are with the job's GitHub OIDC token
  (audience `qbric-ci`); QBRIC checks it names this repository, this workflow and the run QBRIC started.
- **The code under test never gets that token.** Only `announce` and `report` have `id-token: write`;
  the `test` job has `contents: read`.
- **The plan carries the run's variable values**, secrets included, until QBRIC's secret handling is
  decided. Every value is masked in the job log, the `qbric-plan` artifact is kept for one day, and QBRIC
  deletes it once the results are in.
- **The runner only runs what it allows:** `npx`, `npm`, `mvn` and `./mvnw`, without a shell, with files
  and outputs inside the checkout.
- **Third-party actions are pinned to commit SHAs.**

## Requirements

`python3` on the runner (GitHub-hosted runners have it). The workflow sets up Node and Chromium for
Playwright repositories and Java 17 for Maven ones. Set the repository variable `QBRIC_RUNS_ON` to run the
tests on a self-hosted runner, for example one that can reach an internal API.

## Tests

```bash
cd tests && python3 -m unittest
```

The workflow tests need PyYAML; they are skipped without it.
