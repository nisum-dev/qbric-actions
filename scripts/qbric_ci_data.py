"""What a run of API tests reads besides the plan (QBRIC docs/88): its test data and its test users' sign-in.

The data bundle comes from the plan action as a file ({"sha256", "files": {path: text}}); it is written to a folder
outside the checkout and named to every row by QBRIC_DATA_DIR. The sign-in secrets come from the GitHub environment,
which QBRIC wrote before the run; the workflow hands them over as JSON (QBRIC_SECRETS_JSON) and only the names the
plan lists reach the rows. Standard library only.
"""
import json
import os
import pathlib

SECRETS_ENV = "QBRIC_SECRETS_JSON"
DATA_FILE_ENV = "QBRIC_DATA_FILE"


class Missing(Exception):
    """The plan needs something the run was not given."""


def write_bundle(data_file, into):
    """Writes the bundle's files under `into` and returns it; refuses a path that leaves it."""
    try:
        bundle = json.loads(pathlib.Path(data_file).read_text())
    except (OSError, ValueError) as error:
        raise Missing(f"the test data could not be read: {error}") from error
    root = pathlib.Path(into).resolve()
    for rel, text in (bundle.get("files") or {}).items():
        target = (root / rel).resolve()
        if root not in target.parents:
            raise Missing(f"the test data names a file outside its folder: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
    return root


def secrets_for(plan, env):
    """The plan's secrets from the workflow's secrets JSON; a missing one stops the run, named."""
    names = plan.get("secrets") or []
    if not names:
        return {}
    try:
        given = json.loads(env.get(SECRETS_ENV) or "{}")
    except ValueError as error:
        raise Missing(f"the workflow's secrets could not be read: {error}") from error
    missing = [n for n in names if not given.get(n)]
    if missing:
        raise Missing("the GitHub environment has no " + ", ".join(missing) + ": QBRIC writes these before each run, "
                      "so the run's environment must be the one QBRIC dispatched with, and the workflow's execute step "
                      "must pass `secrets: ${{ toJSON(secrets) }}`")
    return {n: given[n] for n in names}


def prepared(plan, env, temp):
    """The plan with each row's variables extended by its test data folder and its secrets; the plan when it has none."""
    extra = {}
    if plan.get("data"):
        data_file = env.get(DATA_FILE_ENV)
        if not data_file or not pathlib.Path(data_file).exists():
            raise Missing("QBRIC planned test data for this run, but none arrived: the plan action fetches it")
        extra["QBRIC_DATA_DIR"] = str(write_bundle(data_file, pathlib.Path(temp) / "qbric-data-dir"))
    extra.update(secrets_for(plan, env))
    if not extra:
        return plan
    rows = [dict(r, env={**extra, **(r.get("env") or {})}) for r in plan["rows"]]
    return dict(plan, rows=rows)
