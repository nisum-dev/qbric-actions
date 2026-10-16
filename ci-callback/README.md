# QBRIC CI callback

Reports a QBRIC-started run back to QBRIC: once when tests are about to run, and once when the
test job has ended. QBRIC uses it to show progress quickly, to learn which commit was tested,
and to read counts from the JUnit results. QBRIC also follows the run on its own, so a missed
callback never leaves a run unfinished.

The action authenticates with the job's GitHub OIDC token, audience `qbric-ci`. QBRIC accepts
it only from the workflow file and branch configured for the repository in Settings → CI/CD.
No secret is stored anywhere.

## Use

The job that calls it needs `permissions: id-token: write`. Never give that permission to the
job that checks out and runs the code under test: that code could then report for the run.
Report from separate jobs instead:

```yaml
jobs:
  announce:
    permissions: { id-token: write }
    steps:
      - uses: nisum-dev/qbric-actions/ci-callback@v2
        with:
          qbric-api-url: ${{ vars.QBRIC_API_URL }}
          tenant-id: ${{ vars.QBRIC_TENANT_ID }}
          correlation-id: ${{ inputs.correlation_id }}
          phase: running

  test:
    needs: announce
    permissions: { contents: read }
    outputs:
      checked_out_sha: ${{ steps.verify.outputs.sha }}
    steps: [] # checkout, verify, run tests, upload qbric-junit and qbric-report

  report:
    needs: test
    if: always()
    permissions: { id-token: write }
    steps:
      - uses: actions/download-artifact@v4
        continue-on-error: true
        with: { name: qbric-junit, path: junit }
      - uses: nisum-dev/qbric-actions/ci-callback@v2
        with:
          qbric-api-url: ${{ vars.QBRIC_API_URL }}
          tenant-id: ${{ vars.QBRIC_TENANT_ID }}
          correlation-id: ${{ inputs.correlation_id }}
          phase: completed
          checked-out-sha: ${{ needs.test.outputs.checked_out_sha }}
          job-outcome: ${{ needs.test.result }}
          junit-dir: junit
```

## Inputs

| Input             | Required | Meaning                                                         |
| ----------------- | -------- | --------------------------------------------------------------- |
| `qbric-api-url`   | yes      | Base URL of qbric-api, https                                    |
| `tenant-id`       | yes      | QBRIC tenant id                                                 |
| `correlation-id`  | yes      | The workflow's `correlation_id` input                           |
| `phase`           | yes      | `running` or `completed`                                        |
| `checked-out-sha` | no       | The commit the tests ran on                                     |
| `job-outcome`     | no       | `completed` only: the test job's result                         |
| `junit-dir`       | no       | `completed` only: folder of JUnit XML files to summarise        |
| `results-file`    | no       | `completed` only: the thin runner's `manifest.json`; its rows are added up instead of `junit-dir` |

The action never fails the build. It needs `python3`, which GitHub-hosted runners provide. The
script it runs, `scripts/qbric_ci_callback.py`, is the same one the Bamboo kit uses; there it
signs with `QBRIC_CALLBACK_SECRET` instead of OIDC.

## Tests

```bash
python3 -m unittest discover -s tests
```
