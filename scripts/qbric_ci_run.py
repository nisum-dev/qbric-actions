#!/usr/bin/env python3
"""Carry out a QBRIC run plan (schema 2) in this checkout: the thin runner.

QBRIC decided everything on its own clone of the same commit: for each datapoint row the files to write, the
variables, the command and the outputs to keep. This script masks the values, runs each row in order without a
shell, retries the way QBRIC's runner does, keeps every attempt's outputs (redacted) and writes manifest.json
after every row, so a run stopped part-way still reports the rows it finished. The plan's timeoutMinutes is the
whole run's limit, as QBRIC's own watchdog counts it. Nothing here decides what to test. Standard library only.

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

import qbric_ci_paths as paths
import qbric_ci_results as results
import qbric_ci_secrets as secrets

PASSED, FAILED, UNUSABLE = 0, 1, 2
SCHEMA = 2
ALLOWED = ("npx", "npm", "mvn", "./mvnw")
STATE_ENV = re.compile(r"^QBRIC_[A-Z0-9_]+$")
NOT_FOUND = 127
UnusablePlan = paths.UnusablePlan


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
        paths.check_path(path)
    state = row.get("stateDirEnv")
    if state is not None and not STATE_ENV.match(state):
        raise UnusablePlan(f"row {row.get('position')} names an unexpected state variable {state!r}")


def run_command(command, workspace, env, timeout_s, log_path, out):
    """Runs without a shell, streaming its output to the log and the job; returns (exit code, ms, timed out)."""
    start = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            proc = subprocess.Popen(command, cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", start_new_session=True)
        except OSError as error:
            message = f"[qbric] {command[0]} could not be started: {error}\n"
            log.write(message)
            out.write(message)
            return NOT_FOUND, int((time.monotonic() - start) * 1000), False
        pump = threading.Thread(target=lambda: [(log.write(l), out.write(l)) for l in proc.stdout], daemon=True)
        pump.start()
        try:
            code, timed_out = proc.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            code, timed_out = proc.wait(), True
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            raise
        pump.join(timeout=10)
        proc.stdout.close()
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


class Deadline:
    """The whole run's time limit: an attempt gets what is left of it, and none starts once it has passed."""

    def __init__(self, minutes, clock=time.monotonic):
        self.minutes, self.clock = minutes, clock
        self.at = clock() + minutes * 60

    def remaining(self):
        return max(0, int(self.at - self.clock()))

    def reason(self):
        return f"the run's time limit of {self.minutes} minutes was reached"


def deadline_of(plan):
    return Deadline(int(plan.get("timeoutMinutes") or 30))  # QBRIC's runner also defaults to 30 minutes


def narrowed(command, files):
    """The row's Playwright command over the failed spec files only, its options unchanged."""
    return command[:3] + [a for a in command[3:] if a.startswith("-")] + [results.file_filter(f) for f in sorted(files)]


def run_row(row, plan, workspace, out_dir, budget, out, run=None, sleep=None, deadline=None):
    """Writes the row's files and state folder, runs its attempts, and removes both whatever happens."""
    written, state = [], None
    try:
        for path, text in (row.get("files") or {}).items():
            target = paths.inside(workspace, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            written.append(target)
        state = tempfile.mkdtemp(prefix=f"qbric-state-{row['position']}-") if row.get("stateDirEnv") else None
        return attempts_of(row, plan, workspace, out_dir, budget, out, run or run_command, sleep or time.sleep,
                           state, deadline or deadline_of(plan))
    finally:
        for target in written:
            target.unlink(missing_ok=True)
        if state:
            shutil.rmtree(state, ignore_errors=True)


def attempts_of(row, plan, workspace, out_dir, budget, out, run, sleep, state, deadline):
    position, outputs = row["position"], row.get("outputs") or []
    retry = plan.get("retry")
    max_attempts = max(2, int(retry.get("maxAttempts", 2))) if retry else 1
    redactor = secrets.Redactor(secrets.values(plan))
    env = dict(os.environ)
    env.update({k: v for k, v in (row.get("env") or {}).items() if v is not None})
    if state:
        env[row["stateDirEnv"]] = state
    command, merged, retrying, attempts, retried, stopped = row["command"], None, None, [], set(), None
    for number in range(1, max_attempts + 1):
        if deadline.remaining() <= 0:
            stopped = deadline.reason()
            break
        paths.clear(workspace, outputs)
        into = out_dir / "rows" / str(position) / f"attempt-{number}"
        into.mkdir(parents=True, exist_ok=True)
        code, ms, timed_out = run(command, workspace, env, deadline.remaining(), into / "output.log", out)
        paths.keep(workspace, outputs, into, out)
        attempt = results.read(workspace)
        redactor.tree(into)
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
    return outcome(row, merged, attempts, retried, stopped)


def outcome(row, merged=None, attempts=(), retried=(), stopped=None):
    counts = merged.counts() if merged else {key: 0 for key in results.KEYS}
    green = merged is not None and merged.green()
    return dict(position=row["position"], datapointId=row.get("datapointId"), green=green, attempts=list(attempts),
                retriedFiles=sorted(retried), stopped=stopped, **counts)


def write_manifest(plan, rows, budget, out_dir, stopped=None):
    manifest = {"schemaVersion": 1, "runId": plan.get("runId"), "correlationId": plan.get("correlationId"),
                "revision": plan.get("revision"), "rows": rows, "retriesUsed": budget.used,
                "maxAttempts": max((len(r["attempts"]) for r in rows), default=0), "stopped": stopped}
    path = out_dir / "manifest.json"
    path.with_suffix(".tmp").write_text(json.dumps(manifest, indent=2))
    path.with_suffix(".tmp").replace(path)


def stop_on_terminate():
    """GitHub stops a cancelled or timed-out job with SIGINT, then SIGTERM; both end the run with its manifest."""
    def stop(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, stop)


def main(argv, env=os.environ, out=sys.stdout):
    parser = argparse.ArgumentParser(description="Run a QBRIC plan in this checkout.")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--out", default=os.path.join(env.get("RUNNER_TEMP", "."), "qbric-results"))
    args = parser.parse_args(argv)
    workspace, out_dir = pathlib.Path(args.workspace).resolve(), pathlib.Path(args.out)
    try:
        plan = load(args.plan)
        paths.check_files(plan, workspace)
    except UnusablePlan as error:
        out.write(f"::error::QBRIC plan not run: {error}\n")
        return UNUSABLE
    secrets.mask(plan, env, out)
    out_dir.mkdir(parents=True, exist_ok=True)
    budget, deadline, rows, stopped, code = Budget(plan.get("retry")), deadline_of(plan), [], None, None
    ordered = sorted(plan["rows"], key=lambda r: r["position"])
    try:
        for row in ordered:
            try:
                rows.append(run_row(row, plan, workspace, out_dir, budget, out, deadline=deadline))
            except UnusablePlan as error:
                out.write(f"::error::QBRIC plan not run: {error}\n")
                stopped, code = f"row {row['position']} could not be used: {error}", UNUSABLE
                break
            out.write(f"[qbric] row {row['position']}: {rows[-1]['passed']}/{rows[-1]['total']} passed\n")
            write_manifest(plan, rows, budget, out_dir)
    except KeyboardInterrupt:
        out.write("::error::QBRIC plan stopped: the job was cancelled or timed out\n")
        stopped, code = "the job was stopped before every row had run", FAILED
    finally:
        done = {r["position"] for r in rows}
        not_run = stopped or "the run ended before this row"
        rows += [outcome(r, stopped=not_run) for r in ordered if r["position"] not in done]
        write_manifest(plan, rows, budget, out_dir, stopped)
    if code is not None:
        return code
    return PASSED if all(r["green"] for r in rows) else FAILED


if __name__ == "__main__":
    stop_on_terminate()
    sys.exit(main(sys.argv[1:]))
