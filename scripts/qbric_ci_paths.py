"""Paths in the checkout: what the plan may write and which outputs are kept. Symbolic links are never followed
out of the checkout. Standard library only.
"""
import os
import pathlib
import shutil


class UnusablePlan(Exception):
    pass


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


def check_files(plan, workspace):
    """Every file the plan writes, through the checkout as it is now, before anything runs."""
    for row in plan["rows"]:
        for path in (row.get("files") or {}):
            inside(workspace, path)


def clear(workspace, paths):
    """Removes an earlier attempt's outputs; a symbolic link is left as it is, and never followed."""
    for path in paths:
        target = workspace / path
        if target.is_symlink():
            continue
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


def keep(workspace, paths, into, out):
    """Copies the outputs that are inside the checkout; a link out of it, or inside a kept folder, is skipped."""
    into.mkdir(parents=True, exist_ok=True)
    root = workspace.resolve()
    for path in paths:
        source = workspace / path
        if not source.exists() and not source.is_symlink():
            continue
        if not source.resolve().is_relative_to(root):
            out.write(f"[qbric] output {path!r} leads outside the checkout; not kept\n")
            continue
        if source.is_dir():
            shutil.copytree(source, into / path, dirs_exist_ok=True,
                            ignore=lambda folder, names: [n for n in names if os.path.islink(os.path.join(folder, n))])
        elif source.is_file():
            (into / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, into / path)
