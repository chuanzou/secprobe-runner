#!/usr/bin/env python3
"""Build the files the `publish` job commits for one evaluation request.

Reads the workflow inputs from INPUT_* env vars and the per-task TaskResult
files downloaded from the `result-<slug>` artifacts, then writes a staging tree:

  <out>/results/<request_id>/request.json
  <out>/results/<request_id>/tasks/<slug>.json      (one per planned task)
  <out>/results/<request_id>/summary.json
and <out-entry> = summary.json copy used for the results/index.json upsert.

Tasks whose job produced no result (cancelled / crashed) get a synthetic
TaskResult with exception "MissingResult: ...". If input validation failed in
`plan`, a `failed` summary is written so the website stops waiting.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (AGENTS_WITHOUT_MODEL, InputError, now_iso, parse_task_list,  # noqa: E402
                    read_json, run_id, run_url, set_output, task_slug, validate_agent,
                    validate_model, validate_request_id, write_json)

INPUTS = ("request_id", "tasks", "agent", "model", "agent_kwargs", "n_attempts", "max_parallel",
          "dataset", "generated_repo")


def _inputs() -> dict[str, str]:
    return {name: os.environ.get(f"INPUT_{name.upper()}", "")[:4000] for name in INPUTS}


def missing_result(task_id: str, agent: str, model: str, reason: str) -> dict[str, Any]:
    return {
        "task_id": task_id, "agent": agent, "model": model,
        "attempts": [{
            "trial": None, "reward": 0, "score": 0.0, "exception": reason,
            "started_at": None, "finished_at": None, "duration_sec": None, "cost_usd": None,
            "tokens": {"input": 0, "cached": 0, "output": 0}, "patch": "",
            "verifier_tail": "", "agent_tail": "",
        }],
        "best_score": 0.0, "passed": False,
    }


def task_exception(result: dict[str, Any]) -> str | None:
    attempts = result.get("attempts") or []
    if any(a.get("reward") == 1 for a in attempts):
        return None
    for attempt in attempts:
        if attempt.get("exception"):
            return attempt["exception"]
    return None


def aggregate(inputs: dict[str, str], results_dir: Path, out: Path, *, plan_result: str,
              run_result: str, started_at: str) -> dict[str, Any] | None:
    try:
        request_id = validate_request_id(inputs["request_id"])
    except InputError as exc:
        print(f"::error title=Not publishing::{exc}")
        return None
    base = out / "results" / request_id
    error = None
    tasks: list[str] = []
    agent, model = inputs.get("agent", "").strip(), inputs.get("model", "").strip()
    try:
        tasks = parse_task_list(inputs["tasks"])
        agent = validate_agent(agent)
        model = validate_model(model, required=agent not in AGENTS_WITHOUT_MODEL)
    except InputError as exc:
        error = f"Invalid inputs: {exc}"
    if plan_result not in ("success", "") and error is None:
        error = f"Planning job {plan_result}"

    request = dict(inputs)
    request.update(run_id=run_id(), run_url=run_url(), started_at=started_at or None)
    write_json(base / "request.json", request)

    if error is not None:
        status = "failed"
        tasks = []
    elif run_result == "cancelled":
        status = "cancelled"
    else:
        status = "completed"

    rows = []
    total_cost = 0.0
    missing = 0
    for task_id in tasks:
        slug = task_slug(task_id)
        result = read_json(results_dir / f"{slug}.json")
        if not isinstance(result, dict) or result.get("task_id") != task_id:
            missing += 1
            reason = ("MissingResult: the run was cancelled before this task finished"
                      if run_result == "cancelled" else
                      "MissingResult: the task job did not produce a result (see the workflow run)")
            result = missing_result(task_id, agent, model, reason)
        write_json(base / "tasks" / f"{slug}.json", result)
        for attempt in result.get("attempts") or []:
            if isinstance(attempt.get("cost_usd"), (int, float)):
                total_cost += attempt["cost_usd"]
        rows.append({
            "task_id": task_id,
            "passed": bool(result.get("passed")),
            "best_score": result.get("best_score") or 0.0,
            "exception": task_exception(result),
            "file": f"tasks/{slug}.json",
        })
    if status == "completed" and missing:
        status = "partial"

    n_tasks = len(rows)
    n_passed = sum(1 for r in rows if r["passed"])
    summary: dict[str, Any] = {
        "request_id": request_id,
        "run_id": run_id(),
        "run_url": run_url(),
        "agent": agent,
        "model": model,
        "status": status,
        "started_at": started_at or None,
        "finished_at": now_iso(),
        "n_tasks": n_tasks,
        "n_passed": n_passed,
        "pass_rate": round(n_passed / n_tasks, 4) if n_tasks else 0.0,
        "mean_score": round(sum(r["best_score"] for r in rows) / n_tasks, 2) if n_tasks else 0.0,
        "total_cost_usd": round(total_cost, 4),
        "tasks": rows,
    }
    if error:
        summary["error"] = error
    write_json(base / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--entry-out", type=Path, required=True,
                        help="where to write the summary used for results/index.json")
    parser.add_argument("--plan-result", default=os.environ.get("PLAN_RESULT", ""))
    parser.add_argument("--run-result", default=os.environ.get("RUN_RESULT", ""))
    parser.add_argument("--started-at", default=os.environ.get("STARTED_AT", ""))
    args = parser.parse_args()
    summary = aggregate(_inputs(), args.results_dir, args.out, plan_result=args.plan_result,
                        run_result=args.run_result, started_at=args.started_at)
    if summary is None:
        set_output("publish", "false")
        return 0
    entry = {k: v for k, v in summary.items() if k != "tasks"}
    entry["tasks"] = summary["tasks"]
    write_json(args.entry_out, entry)
    set_output("publish", "true")
    set_output("request_id", summary["request_id"])
    print(f"{summary['request_id']}: status={summary['status']} passed={summary['n_passed']}/"
          f"{summary['n_tasks']} mean_score={summary['mean_score']} cost=${summary['total_cost_usd']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
