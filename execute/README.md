# QBRIC execute

Runs the plan the [plan action](../plan/README.md) uploaded, in this checkout, with the thin runner
(`scripts/qbric_ci_run.py`, Python standard library only). QBRIC decided everything on its own clone of
the same commit: for each datapoint row the files to write, the variables, the command and the outputs
to keep. The runner masks every value, runs each row in order without a shell, retries failed tests the
way QBRIC's runner does, and keeps every attempt's output. Nothing is downloaded and no QBRIC code runs
here.

It uploads `qbric-results` (`manifest.json` and the raw Playwright JSON or Surefire XML, which QBRIC
parses on import), `qbric-report` (the HTML reports) and `qbric-logs` (each attempt's output), even when
tests fail. The step fails when a test fails.

```yaml
jobs:
  test:
    needs: announce
    permissions: { contents: read }
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
        with: { ref: "${{ inputs.sha }}", persist-credentials: false }
      - uses: nisum-dev/qbric-actions/execute@v3
```

Never give this job `id-token: write`: it runs the code under test.

| Input | Default | Meaning |
| --- | --- | --- |
| `workspace` | `.` | The checkout, at the commit QBRIC asked for |
| `plan-artifact` | `qbric-plan` | The artifact the plan action uploaded |

## What the runner allows

- Commands: `npx`, `npm`, `mvn` and `./mvnw` only, as argument lists, never through a shell.
- Files and outputs: paths inside the checkout only; a path that leaves it, directly or through a
  symbolic link, stops the run before anything runs.
- Exit codes: 0 every row passed, 1 a test failed, 2 the plan could not be used.

## What the runner needs

`python3` (every GitHub-hosted runner has it), plus Node and Chromium for Playwright repositories or
Java 17 and Maven for REST Assured ones. The [managed workflow](../templates/qbric-ci-managed.yml) sets
these up from the checkout (`package.json` or `pom.xml`). A self-hosted runner (`QBRIC_RUNS_ON`) must
provide `python3` itself.
