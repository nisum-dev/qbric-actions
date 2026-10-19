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
  decided. Every value is masked in the job log. The `qbric-plan` artifact is kept for one day at most:
  QBRIC deletes it as soon as the run ends, however it ends.
- **Kept outputs are redacted.** Before upload, every plan value of four characters or more is replaced
  by `***` in the logs and in the text of the Playwright JSON, the Surefire XML and the HTML report.
  Binary files (screenshots, traces, the zipped data inside Playwright's HTML report) cannot be
  redacted, so `qbric-results`, `qbric-report` and `qbric-logs` are kept for seven days.
- **The runner refuses obviously harmful plans:** commands other than `npx`, `npm`, `mvn` and `./mvnw`,
  a shell, and any file or output outside the checkout, directly or through a symbolic link.
- **Third-party actions are pinned to commit SHAs.**

### Trust boundary

The runner's checks limit mistakes; they do not make QBRIC untrusted. A plan's files include the
Playwright wrapper config, which Playwright executes, and `npx` and `npm` run whatever package they are
given. Whoever controls QBRIC, or this repository's `v3` tag, can run code on the runner, as can anyone
who can push to the test repository. QBRIC already writes to the test repository through its GitHub App,
so this is the trust the repository gives QBRIC anyway. Keep secrets the tests do not need out of the
runner, and pin `@<commit SHA>` instead of `@v3` when the moving tag is not acceptable.

## Requirements

`python3` on the runner (GitHub-hosted runners have it). The workflow sets up Node and Chromium for
Playwright repositories and Java 17 for Maven ones. Set the repository variable `QBRIC_RUNS_ON` to run the
tests on a self-hosted runner, for example one that can reach an internal API.

## Tests

```bash
cd tests && python3 -m unittest
```

The workflow tests need PyYAML; they are skipped without it.
