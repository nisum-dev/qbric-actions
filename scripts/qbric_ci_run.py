#!/usr/bin/env python3
"""Carry out a QBRIC run plan (schema 2) in this checkout: the thin runner.

QBRIC decided everything on its own clone of the same commit: for each datapoint row the files to write, the
variables, the command and the outputs to keep. This script masks the values, runs each row in order without a
shell, retries the way QBRIC's runner does, keeps every attempt's outputs and writes manifest.json. Nothing here
decides what to test. Standard library only.

  qbric_ci_run.py --plan <qbric-plan.json> [--workspace <dir>] [--out <dir>]

Exit 0 when every row passed, 1 when a test failed, 2 when the plan cannot be used.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import qbric_ci_results as results

PASSED, FAILED, UNUSABLE = 0, 1, 2
SCHEMA = 2
ALLOWED = ("npx", "npm", "mvn", "./mvnw")
STATE_ENV = re.compile(r"^QBRIC_[A-Z0-9_]+$")


class UnusablePlan(Exception):
    pass


def load(path):
    try:
        plan = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError) as error:
        raise UnusablePlan(f"the plan could not be read: {error}") from error
    if plan.get("schemaVersion") != SCHEMA:
        raise UnusablePlan(f"this runner reads plan schema {SCHEMA}, the plan is schema {plan.get('schemaVersion')}")
    if not plan.get("rows"):
        raise UnusablePlan("the plan has no rows")
    for row in plan["rows"]:
        check_row(row)
    return plan


def check_row(row):
    command = row.get("command") or []
    if not command or not all(isinstance(a, str) for a in command):
        raise UnusablePlan(f"row {row.get('position')} has no command")
    if command[0] not in ALLOWED:
        raise UnusablePlan(f"row {row.get('position')} runs {command[0]!r}, which is not one of {', '.join(ALLOWED)}")
    for path in list((row.get("files") or {}).keys()) + list(row.get("outputs") or []):
        check_path(path)
    state = row.get("stateDirEnv")
    if state is not None and not STATE_ENV.match(state):
        raise UnusablePlan(f"row {row.get('position')} names an unexpected state variable {state!r}")


def check_path(path):
    p = pathlib.PurePosixPath(path)
    if not path or p.is_absolute() or ".." in p.parts or "\\" in path:
        raise UnusablePlan(f"{path!r} is not a path inside the checkout")


def inside(workspace, path):
    """The path in the checkout, refusing one that a symbolic link leads out of it."""
    root = workspace.resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise UnusablePlan(f"{path!r} leads outside the checkout")
    return target


def mask(plan, env, out):
    """Hide every variable value in GitHub's log before anything else is printed."""
    if env.get("GITHUB_ACTIONS") != "true":
        return
    values = {v for row in plan["rows"] for v in (row.get("env") or {}).values() if v}
    for value in sorted(values):
        for line in value.splitlines():
            if line.strip():
                out.write("::add-mask::" + line.replace("%", "%25").replace("\r", "%0D") + "\n")
    out.flush()


def run_command(command, workspace, env, timeout_s, log_path, out):
    """Runs without a shell, streaming its output to the log and the job; returns (exit code, ms, timed out)."""
    start = time.monotonic()
    with open(log_path, "w") as log:
        proc = subprocess.Popen(command, cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        pump = threading.Thread(target=lambda: [(log.write(l), out.write(l)) for l in proc.stdout], daemon=True)
        pump.start()
        try:
            code, timed_out = proc.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            code, timed_out = proc.wait(), True
        pump.join(timeout=10)
    return code, int((time.monotonic() - start) * 1000), timed_out


class Budget:
    """Retries across the whole run, as QBRIC's RetryBudget: a count and a time limit."""

    def __init__(self, retry, clock=time.monotonic):
        self.clock = clock
        self.max_retries = max(0, int(retry.get("maxRetriesPerRun", 0))) if retry else 0
        self.deadline = clock() + (int(retry.get("budgetSeconds", 0)) if retry else 0)
        self.used = 0

    def take(self):
        if self.used >= self.max_retries:
            return f"run-level retry budget spent ({self.used}/{self.max_retries})"
        if self.clock() >= self.deadline:
            return "run-level time budget exhausted"
        self.used += 1
        return None


def narrowed(command, files):
    """The row's Playwright command over the failed spec files only, its options unchanged."""
    return command[:3] + [a for a in command[3:] if a.startswith("-")] + [results.file_filter(f) for f in sorted(files)]


def clear(workspace, paths):
    for path in paths:
        target = workspace / path
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


def keep(workspace, paths, into):
    into.mkdir(parents=True, exist_ok=True)
    for path in paths:
        source = workspace / path
        if source.is_dir():
            shutil.copytree(source, into / path, dirs_exist_ok=True)
        elif source.is_file():
            (into / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, into / path)


def run_row(row, plan, workspace, out_dir, budget, out, run=None, sleep=None):
    """Writes the row's files and state folder, runs its attempts, and removes both whatever happens."""
    written, state = [], None
    try:
        for path, text in (row.get("files") or {}).items():
            target = inside(workspace, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            written.append(target)
        state = tempfile.mkdtemp(prefix=f"qbric-state-{row['position']}-") if row.get("stateDirEnv") else None
        return attempts_of(row, plan, workspace, out_dir, budget, out, run or run_command, sleep or time.sleep,
                           state)
    finally:
        for target in written:
            target.unlink(missing_ok=True)
        if state:
            shutil.rmtree(state, ignore_errors=True)


def attempts_of(row, plan, workspace, out_dir, budget, out, run, sleep, state):
    position, outputs = row["position"], row.get("outputs") or []
    retry = plan.get("retry")
    max_attempts = max(2, int(retry.get("maxAttempts", 2))) if retry else 1
    timeout_s = int(plan.get("timeoutMinutes") or 30) * 60  # QBRIC's runner also defaults to 30 minutes
    env = dict(os.environ)
    env.update({k: v for k, v in (row.get("env") or {}).items() if v is not None})
    if state:
        env[row["stateDirEnv"]] = state
    command, merged, retrying, attempts, retried, stopped = row["command"], None, None, [], set(), None
    for number in range(1, max_attempts + 1):
        clear(workspace, outputs)
        into = out_dir / "rows" / str(position) / f"attempt-{number}"
        into.mkdir(parents=True, exist_ok=True)
        code, ms, timed_out = run(command, workspace, env, timeout_s, into / "output.log", out)
        keep(workspace, outputs, into)
        attempt = results.read(workspace)
        attempts.append({"number": number, "exitCode": code, "durationMs": ms, "timedOut": timed_out,
                         "narrowedTo": sorted(retrying) if retrying else None, "results": attempt is not None})
        if attempt is None:
            if retry:
                stopped = "the attempt wrote no results (a compile error or a crash); a retry cannot change that"
            break
        merged = attempt if retrying is None or merged is None else merged.merged_with(attempt, retrying)
        if merged.green():
            break
        if number >= max_attempts:
            break
        stopped = budget.take()
        if stopped:
            break
        files = merged.failed_files() if command[:3] == ["npx", "playwright", "test"] else None
        retrying = files
        retried |= files if files else merged.files()
        command = narrowed(row["command"], files) if files else row["command"]
        sleep(max(0, int(retry.get("cooldownSeconds", 0))))
    counts = merged.counts() if merged else {key: 0 for key in results.KEYS}
    green = merged is not None and merged.green()
    return dict(position=position, datapointId=row.get("datapointId"), green=green, attempts=attempts,
                retriedFiles=sorted(retried), stopped=stopped, **counts)


def main(argv, env=os.environ, out=sys.stdout):
    parser = argparse.ArgumentParser(description="Run a QBRIC plan in this checkout.")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--out", default=os.path.join(env.get("RUNNER_TEMP", "."), "qbric-results"))
    args = parser.parse_args(argv)
    try:
        plan = load(args.plan)
    except UnusablePlan as error:
        out.write(f"::error::QBRIC plan not run: {error}\n")
        return UNUSABLE
    mask(plan, env, out)
    workspace, out_dir = pathlib.Path(args.workspace).resolve(), pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    budget, rows = Budget(plan.get("retry")), []
    for row in sorted(plan["rows"], key=lambda r: r["position"]):
        try:
            rows.append(run_row(row, plan, workspace, out_dir, budget, out))
        except UnusablePlan as error:
            out.write(f"::error::QBRIC plan not run: {error}\n")
            return UNUSABLE
        out.write(f"[qbric] row {row['position']}: {rows[-1]['passed']}/{rows[-1]['total']} passed\n")
    manifest = {"schemaVersion": 1, "runId": plan.get("runId"), "correlationId": plan.get("correlationId"),
                "revision": plan.get("revision"), "rows": rows, "retriesUsed": budget.used,
                "maxAttempts": max(len(r["attempts"]) for r in rows)}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return PASSED if all(r["green"] for r in rows) else FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
