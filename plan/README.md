# QBRIC run plan

Fetches the plan QBRIC made for a run of a QBRIC-managed repository and uploads it as the
`qbric-plan` artifact (kept one day) for the job that runs the tests. The plan names the cases to
run, every datapoint's variables, the order and the shared login.

It authenticates like the callback: the job's GitHub OIDC token (audience `qbric-ci`), accepted
only from the configured workflow, and only for the workflow run QBRIC started for this plan.
A plan that cannot be fetched fails the job.

```yaml
jobs:
  announce:
    permissions: { id-token: write }
    steps:
      - uses: nisum-dev/qbric-actions/plan@v3
        with:
          qbric-api-url: ${{ vars.QBRIC_API_URL }}
          tenant-id: ${{ vars.QBRIC_TENANT_ID }}
          correlation-id: ${{ inputs.correlation_id }}
```

| Input | Required | Meaning |
| --- | --- | --- |
| `qbric-api-url` | yes | Base URL of qbric-api, https |
| `tenant-id` | yes | QBRIC tenant id |
| `correlation-id` | yes | The workflow's `correlation_id` input |

The plan holds every variable value of the run, secrets included, until QBRIC's secret handling
is decided (docs/22, D4). It is written owner-only, never printed, and the artifact expires after
one day. Anyone with read access to the repository can download it until then.
