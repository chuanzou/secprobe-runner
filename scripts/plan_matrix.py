#!/usr/bin/env python3
"""Validate evaluate.yml workflow_dispatch inputs and emit job parameters.

Inputs are read from environment variables (INPUT_<NAME>) — never from the
command line — so the workflow never interpolates user text into a shell.

    python scripts/plan_matrix.py evaluate   # -> outputs: matrix, max_parallel, n_attempts, started_at
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (AGENTS_WITHOUT_MODEL, DEFAULT_GENERATED_REPO, InputError,  # noqa: E402
                    kwargs_to_ak, now_iso, parse_agent_kwargs, parse_task_list, set_output,
                    task_slug, validate_agent, validate_dataset, validate_generated_repo,
                    validate_int, validate_model, validate_request_id)


def env(name: str, default: str = "") -> str:
    return os.environ.get(f"INPUT_{name.upper()}", default)


# ----------------------------------------------------------------- evaluate

def plan_evaluate(values: dict[str, str]) -> dict[str, Any]:
    request_id = validate_request_id(values.get("request_id", ""))
    tasks = parse_task_list(values.get("tasks", ""))
    agent = validate_agent(values.get("agent", ""))
    model = validate_model(values.get("model", ""), required=agent not in AGENTS_WITHOUT_MODEL)
    kwargs = parse_agent_kwargs(values.get("agent_kwargs", ""))
    n_attempts = validate_int("n_attempts", values.get("n_attempts", "1") or "1", 1, 5)
    max_parallel = validate_int("max_parallel", values.get("max_parallel", "4") or "4", 1, 64)
    dataset = validate_dataset(values.get("dataset", "") or "xluobd/SecProbe-353")
    generated_repo = validate_generated_repo(values.get("generated_repo", "") or DEFAULT_GENERATED_REPO)
    return {
        "request_id": request_id,
        "tasks": tasks,
        "agent": agent,
        "model": model,
        "agent_kwargs": kwargs,
        "ak": kwargs_to_ak(kwargs),
        "n_attempts": n_attempts,
        "max_parallel": max_parallel,
        "dataset": dataset,
        "generated_repo": generated_repo,
        "matrix": {"include": [{"task": t, "slug": task_slug(t)} for t in tasks]},
    }


INPUT_NAMES = {
    "evaluate": ("request_id", "tasks", "agent", "model", "agent_kwargs", "n_attempts",
                 "max_parallel", "dataset", "generated_repo"),
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in INPUT_NAMES:
        print("usage: plan_matrix.py evaluate", file=sys.stderr)
        return 2
    mode = argv[1]
    values = {name: env(name) for name in INPUT_NAMES[mode]}
    try:
        plan = plan_evaluate(values)
    except InputError as exc:
        # Messages never echo full user input, only bounded excerpts.
        print(f"::error title=Invalid input::{exc}")
        return 1
    set_output("matrix", json.dumps(plan["matrix"], separators=(",", ":")))
    set_output("max_parallel", str(plan["max_parallel"]))
    set_output("n_attempts", str(plan["n_attempts"]))
    set_output("started_at", now_iso())
    print(f"Validated {len(plan['tasks'])} task(s); agent={plan['agent']}; "
          f"attempts={plan['n_attempts']}; max_parallel={plan['max_parallel']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
