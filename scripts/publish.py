#!/usr/bin/env python3
"""Commit files to the orphan `secprobe-results` branch, safely under concurrency.

Each attempt starts from the *current* remote tip in a throw-away git worktree
(so the main checkout is never touched), copies the staged files in, applies
index upserts (read-modify-write of JSON arrays), commits and pushes. A rejected
push (another run published first) simply retries from the new tip, up to 5x.
The branch is created as an orphan if it does not exist yet.

    python scripts/publish.py --repo . --staging DIR --message "..." \
        [--upsert results/index.json=path/to/summary.json] [--limit 500]

Within one runner, publishers serialise on a lock file in the git dir, so two
publish steps on the same machine never race locally.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import RESULTS_BRANCH, read_json  # noqa: E402

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
INDEX_KEY = "request_id"


class PublishError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-800:]
        raise PublishError(f"git {args[0]} failed ({proc.returncode}): {detail}")
    if not quiet and proc.returncode != 0:
        print((proc.stderr or "").strip()[-400:], file=sys.stderr)
    return proc


@contextlib.contextmanager
def local_lock(repo: Path) -> Iterator[None]:
    common_dir = git(repo, "rev-parse", "--git-common-dir").stdout.strip()
    path = Path(common_dir)
    if not path.is_absolute():
        path = repo / path
    with open(path / "secprobe-publish.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def upsert_index(path: Path, entry: dict[str, Any], limit: int) -> None:
    current = read_json(path, [])
    if not isinstance(current, list):
        current = []
    key = entry.get(INDEX_KEY)
    rest = [e for e in current if not (isinstance(e, dict) and e.get(INDEX_KEY) == key)]
    merged = [entry] + rest
    # Most recent first; Python's sort is stable so ties keep "newest publish first".
    merged.sort(key=lambda e: str(e.get("started_at") or e.get("finished_at")
                                  or e.get("updated_at") or "") if isinstance(e, dict) else "",
                reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged[:limit], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _remote_branch_exists(repo: Path, remote: str, branch: str) -> bool:
    proc = git(repo, "ls-remote", "--exit-code", "--heads", remote, branch, check=False, quiet=True)
    if proc.returncode == 0:
        return True
    if proc.returncode == 2:
        return False
    raise PublishError(f"git ls-remote failed: {proc.stderr.strip()[-400:]}")


def publish_once(repo: Path, staging: Path | None, message: str,
                 upserts: list[tuple[str, dict[str, Any]]], *, remote: str, branch: str,
                 limit: int) -> bool:
    """Returns True when a commit was pushed, False when there was nothing to change."""
    exists = _remote_branch_exists(repo, remote, branch)
    tracking = f"refs/remotes/{remote}/{branch}"
    if exists:
        git(repo, "fetch", "--quiet", "--no-tags", "--depth", "1", remote,
            f"+refs/heads/{branch}:{tracking}")
    workdir = Path(tempfile.mkdtemp(prefix="secprobe-publish-"))
    worktree = workdir / "wt"
    try:
        if exists:
            git(repo, "worktree", "add", "--quiet", "--detach", str(worktree), tracking)
        else:
            git(repo, "worktree", "add", "--quiet", "--detach", str(worktree), "HEAD")
            git(worktree, "checkout", "--quiet", "--orphan", f"secprobe-orphan-{random.getrandbits(32):08x}")
            git(worktree, "rm", "-rfq", "--cached", ".", check=False, quiet=True)
            for child in worktree.iterdir():
                if child.name == ".git":
                    continue
                shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        if staging is not None and staging.is_dir():
            shutil.copytree(staging, worktree, dirs_exist_ok=True, symlinks=False)
        for rel, entry in upserts:
            upsert_index(worktree / rel, entry, limit)
        git(worktree, "add", "-A")
        if git(worktree, "diff", "--cached", "--quiet", check=False, quiet=True).returncode == 0:
            print("publish: nothing to commit")
            return False
        git(worktree, "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
            "commit", "--quiet", "--no-verify", "-m", message)
        push = git(worktree, "push", "--quiet", remote, f"HEAD:refs/heads/{branch}",
                   check=False, quiet=True)
        if push.returncode != 0:
            raise PublishError(f"push rejected: {push.stderr.strip()[-400:]}")
        return True
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree), check=False, quiet=True)
        shutil.rmtree(workdir, ignore_errors=True)
        git(repo, "worktree", "prune", check=False, quiet=True)


def publish(repo: Path, staging: Path | None, message: str,
            upserts: list[tuple[str, dict[str, Any]]] | None = None, *, remote: str = "origin",
            branch: str = RESULTS_BRANCH, limit: int = 500, attempts: int = 5) -> bool:
    repo = repo.resolve()
    last: Exception | None = None
    with local_lock(repo):
        for attempt in range(1, attempts + 1):
            try:
                return publish_once(repo, staging, message, list(upserts or []), remote=remote,
                                    branch=branch, limit=limit)
            except PublishError as exc:
                last = exc
                print(f"publish attempt {attempt}/{attempts} failed: {exc}", file=sys.stderr)
                if attempt < attempts:
                    time.sleep(random.uniform(1.0, 3.0) * attempt)
    raise PublishError(f"giving up after {attempts} attempts: {last}")


def parse_upsert(spec: str) -> tuple[str, dict[str, Any]]:
    rel, _, source = spec.partition("=")
    if not rel or not source or rel.startswith("/") or ".." in Path(rel).parts:
        raise SystemExit(f"bad --upsert spec: {spec}")
    entry = read_json(Path(source))
    if not isinstance(entry, dict) or INDEX_KEY not in entry:
        raise SystemExit(f"--upsert source must be a JSON object with {INDEX_KEY}: {source}")
    return rel, entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--message", required=True)
    parser.add_argument("--upsert", action="append", default=[],
                        help="REL_PATH=ENTRY.json: upsert ENTRY into the JSON array at REL_PATH")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default=RESULTS_BRANCH)
    args = parser.parse_args()
    upserts = [parse_upsert(spec) for spec in args.upsert]
    try:
        pushed = publish(args.repo, args.staging, args.message, upserts, remote=args.remote,
                         branch=args.branch, limit=args.limit)
    except PublishError as exc:
        print(f"::error title=Publishing results failed::{exc}")
        return 1
    print("publish: pushed" if pushed else "publish: up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
