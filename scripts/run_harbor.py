#!/usr/bin/env python3
"""Run `harbor run` for one task with inputs taken from the environment.

Inputs are re-validated here (defence in depth) and passed to Harbor as an argv
list, so no user-controlled text ever reaches a shell. Harbor's own exit code is
recorded in <jobs-dir>/harbor-exit-code and the console output in
<jobs-dir>/harbor.log; this script exits 0 so result collection always runs.

Env: INPUT_AGENT, INPUT_MODEL, INPUT_AGENT_KWARGS, INPUT_N_ATTEMPTS
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (AGENTS_WITHOUT_MODEL, SECRET_NAMES, InputError, kwargs_to_ak,  # noqa: E402
                    parse_agent_kwargs, redact, set_output, validate_agent, validate_int,
                    validate_model)


def build_command(harbor: str, task_dir: Path, jobs_dir: Path, job_name: str, *, agent: str,
                  model: str, kwargs: dict, n_attempts: int) -> list[str]:
    command = [harbor, "run", "-p", str(task_dir), "-a", agent,
               "-k", str(n_attempts), "-n", str(n_attempts),
               "-o", str(jobs_dir), "--job-name", job_name,
               "--artifact", "/app", "-y"]
    if model:
        command += ["-m", model]
    for item in kwargs_to_ak(kwargs):
        command += ["--ak", item]
    return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--job-name", default="job")
    parser.add_argument("--harbor", default=shutil.which("harbor") or "harbor")
    args = parser.parse_args()

    try:
        agent = validate_agent(os.environ.get("INPUT_AGENT", ""))
        model = validate_model(os.environ.get("INPUT_MODEL", ""),
                               required=agent not in AGENTS_WITHOUT_MODEL)
        kwargs = parse_agent_kwargs(os.environ.get("INPUT_AGENT_KWARGS", ""))
        n_attempts = validate_int("n_attempts", os.environ.get("INPUT_N_ATTEMPTS", "1") or "1", 1, 5)
    except InputError as exc:
        print(f"::error title=Invalid input::{exc}")
        return 1

    args.jobs_dir.mkdir(parents=True, exist_ok=True)
    command = build_command(args.harbor, args.task_dir, args.jobs_dir, args.job_name, agent=agent,
                            model=model, kwargs=kwargs, n_attempts=n_attempts)
    print("+ " + " ".join(command[1:]), flush=True)

    # Empty secrets (unset repo secrets) must look unset to agents / LiteLLM.
    child_env = {k: v for k, v in os.environ.items() if not (k in SECRET_NAMES and not v)}
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    log_path = args.jobs_dir / "harbor.log"
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(command, env=child_env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace")
        assert proc.stdout is not None
        for line in proc.stdout:
            line = redact(line)
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
        code = proc.wait()
    (args.jobs_dir / "harbor-exit-code").write_text(f"{code}\n")
    set_output("harbor_exit", str(code))
    if code != 0:
        print(f"::warning title=Harbor exited with {code}::results will still be collected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
