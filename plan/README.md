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

| Output | Meaning |
| --- | --- |
| `java-version` | The Java QBRIC read from the build at the tested commit (Maven or Gradle, never below 17); empty when the project has no such build. The managed workflow's announce job passes it to the test job's Set up Java. |

The plan holds every variable value of the run, secrets included, until QBRIC's secret handling
is decided (docs/22, D4). It is written owner-only and never printed. QBRIC deletes the artifact as
soon as the run ends, however it ends, and it expires after one day in any case; anyone with read
access to the repository can download it until then. A re-run replaces it.
