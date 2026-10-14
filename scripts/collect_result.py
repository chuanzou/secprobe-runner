#!/usr/bin/env python3
"""Turn one Harbor job directory into a SecProbe `TaskResult` JSON (see CONTRACT.md §3).

Harbor 0.20 job layout (observed):
  <job>/result.json, config.json, job.log          job level (ignored here)
  <job>/<trial>/result.json                       one per attempt: verifier_result.rewards,
                                                  agent_result.{cost_usd,n_input_tokens,...},
                                                  exception_info, started_at, finished_at
  <job>/<trial>/verifier/{grade.json,reward.json,test-stdout.txt,diagnostic.txt}
  <job>/<trial>/agent/...                         agent logs (terminus_2.pane, <agent>.txt, ...)
  <job>/<trial>/artifacts/manifest.json           `--artifact /app` lands in artifacts/app/
  <job>/<trial>/trial.log, exception.txt

usage: collect_result.py --job-dir J --task-dir T --task-id ID --agent A --model M --out OUT.json
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (parse_iso, read_json, read_tail, redact, strip_ansi, tail_lines,  # noqa: E402
                    truncate_bytes, write_json)

PATCH_LIMIT = 300 * 1024
TAIL_LINES = 150
EXCLUDED_DIRS = {
    ".git", "build", "dist", "target", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".venv", "venv", ".tox", ".gradle", ".cache", "CMakeFiles",
    ".next", ".turbo", "coverage", ".nyc_output", "htmlcov", ".eggs",
}
EXCLUDED_SUFFIXES = {
    ".pyc", ".pyo", ".o", ".obj", ".a", ".so", ".dylib", ".class", ".d", ".gcda", ".gcno",
    ".tsbuildinfo",
}
EXCLUDED_NAMES = {".coverage", ".DS_Store", "CMakeCache.txt", "cmake_install.cmake"}
MAX_DIFF_FILE = 2 * 1024 * 1024
KNOWN_AGENT_LOGS = (
    "terminus_2.pane", "mini-swe-agent.txt", "claude-code.txt", "codex.txt", "gemini-cli.txt",
    "openhands.txt", "openhands_sdk.txt", "aider.txt", "goose.txt", "opencode.txt",
    "qwen-code.txt", "oracle.txt", "terminus-json-plain.txt", "terminus-xml-plain.txt",
)
SKIP_AGENT_LOGS = {"instruction.txt", "prompt.txt", "solution.txt", "response.txt"}


# ------------------------------------------------------------------ patch

def _excluded(rel: Path) -> bool:
    if any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in rel.parts[:-1]):
        return True
    return rel.suffix in EXCLUDED_SUFFIXES or rel.name in EXCLUDED_NAMES


def list_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    if not root.is_dir():
        return files
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames
                             if d not in EXCLUDED_DIRS and not d.endswith(".egg-info")
                             and not (base / d).is_symlink())
        for name in filenames:
            path = base / name
            rel = path.relative_to(root)
            if _excluded(rel) or not path.is_file() or path.is_symlink():
                continue
            files[rel.as_posix()] = path
    return files


def _read_text(path: Path | None) -> tuple[list[str] | None, bytes]:
    if path is None:
        return [], b""
    data = path.read_bytes()
    if len(data) > MAX_DIFF_FILE or b"\x00" in data[:8192]:
        return None, data
    try:
        return data.decode("utf-8").splitlines(keepends=True), data
    except UnicodeDecodeError:
        return None, data


def unified_patch(original: Path, modified: Path) -> str:
    before, after = list_files(original), list_files(modified)
    chunks: list[str] = []
    for rel in sorted(set(before) | set(after)):
        a_path, b_path = before.get(rel), after.get(rel)
        a_lines, a_raw = _read_text(a_path)
        b_lines, b_raw = _read_text(b_path)
        if a_raw == b_raw and a_path is not None and b_path is not None:
            continue
        header = f"diff --git a/{rel} b/{rel}\n"
        if a_path is None:
            header += "new file mode 100644\n"
        elif b_path is None:
            header += "deleted file mode 100644\n"
        if a_lines is None or b_lines is None:
            chunks.append(header + f"Binary files a/{rel} and b/{rel} differ\n")
            continue
        diff = difflib.unified_diff(a_lines, b_lines,
                                    "/dev/null" if a_path is None else f"a/{rel}",
                                    "/dev/null" if b_path is None else f"b/{rel}", n=3)
        body = []
        for line in diff:
            body.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        if body:
            chunks.append(header + "".join(body))
    return "".join(chunks)


def find_app_artifact(trial: Path) -> Path | None:
    manifest = read_json(trial / "artifacts" / "manifest.json", [])
    if isinstance(manifest, list):
        for entry in manifest:
            if (isinstance(entry, dict) and entry.get("source") in ("/app", "/app/")
                    and entry.get("status") == "ok"):
                dest = trial / str(entry.get("destination") or "artifacts/app")
                if dest.is_dir():
                    return dest
    fallback = trial / "artifacts" / "app"
    return fallback if fallback.is_dir() else None


# ------------------------------------------------------------------ tails

def _fmt(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def verifier_tail(trial: Path) -> str:
    verifier = trial / "verifier"
    header: list[str] = []
    grade = read_json(verifier / "grade.json")
    if isinstance(grade, dict):
        header.append(f"grade: score={_fmt(grade.get('score'))} / {_fmt(grade.get('max_score', 100))}")
        categories = grade.get("categories")
        if isinstance(categories, dict):  # {"name": {...}}
            items = list(categories.items())
        elif isinstance(categories, list):  # [{"name": ..., ...}]
            items = [(c.get("name"), c) for c in categories if isinstance(c, dict)]
        else:
            items = []
        for name, cat in items[:30]:
            if isinstance(cat, dict):
                header.append(f"  {name}: {_fmt(cat.get('score'))}/{_fmt(cat.get('max_score'))} "
                              f"({cat.get('passed')}/{cat.get('tests')} tests passed)")
        failures = grade.get("failures")
        if isinstance(failures, list) and failures:
            header.append(f"failing hidden tests: {len(failures)}")
    reward = read_json(verifier / "reward.json")
    if isinstance(reward, dict):
        header.append("reward: " + json.dumps(reward, sort_keys=True))
    budget = max(20, TAIL_LINES - len(header))
    body: list[str] = []
    for name in ("diagnostic.txt", "test-stdout.txt"):
        path = verifier / name
        if path.is_file() and path.stat().st_size:
            body.append(f"--- {name} ---")
            body.append(read_tail(path, budget))
    text = "\n".join(header)
    if body:
        text += ("\n" if text else "") + tail_lines("\n".join(body), budget)
    return text


def agent_log_path(trial: Path) -> Path | None:
    agent = trial / "agent"
    if agent.is_dir():
        for name in KNOWN_AGENT_LOGS:
            if (agent / name).is_file():
                return agent / name
        candidates = [p for p in agent.rglob("*")
                      if p.is_file() and p.suffix in (".txt", ".pane", ".log")
                      and p.name not in SKIP_AGENT_LOGS]
        stdout = sorted((p for p in candidates if p.name == "stdout.txt"),
                        key=lambda p: p.stat().st_mtime)
        if stdout:
            return stdout[-1]
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_size)
    trial_log = trial / "trial.log"
    return trial_log if trial_log.is_file() else None


def agent_tail(trial: Path) -> str:
    path = agent_log_path(trial)
    return read_tail(path, TAIL_LINES) if path else ""


# ------------------------------------------------------------------ attempts

def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and value == value:
        return float(value)
    return None


def provider_cost_from_trajectory(trial: Path) -> float | None:
    """mini-swe-agent via OpenRouter: sum provider-reported usage.cost (if every call has one)."""
    raw = read_json(trial / "agent" / "mini-swe-agent.trajectory.json")
    if not isinstance(raw, dict):
        return None
    total, calls, priced = 0.0, 0, 0
    for message in raw.get("messages") or []:
        usage = (((message or {}).get("extra") or {}).get("response") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        calls += 1
        cost = _num(usage.get("cost"))
        if cost is not None:
            priced += 1
            total += cost
    return total if calls and priced == calls else None


def parse_attempt(trial: Path, original: Path | None) -> dict[str, Any]:
    result = read_json(trial / "result.json", {}) or {}
    rewards = ((result.get("verifier_result") or {}).get("rewards") or {})
    reward_value = _num(rewards.get("reward"))
    score = _num(rewards.get("score_100"))
    if score is None:
        score = _num(rewards.get("score"))
    grade = read_json(trial / "verifier" / "grade.json")
    if score is None and isinstance(grade, dict):
        score = _num(grade.get("score"))
    if reward_value is None:
        reward_file = read_json(trial / "verifier" / "reward.json")
        if isinstance(reward_file, dict):
            reward_value = _num(reward_file.get("reward"))
            if score is None:
                score = _num(reward_file.get("score_100"))
    if score is None:
        score = 100.0 if reward_value == 1.0 else 0.0
    reward = 1 if reward_value is not None and reward_value >= 1.0 else 0

    exception = None
    info = result.get("exception_info")
    if isinstance(info, dict):
        message = str(info.get("exception_message") or "").strip().splitlines()
        exception = f"{info.get('exception_type') or 'Exception'}: {message[0] if message else ''}"
        exception = exception.strip()[:500]
    elif (trial / "exception.txt").is_file():
        lines = [l for l in read_tail(trial / "exception.txt", 5).splitlines() if l.strip()]
        exception = (lines[-1] if lines else "Exception")[:500]

    started, finished = parse_iso(result.get("started_at")), parse_iso(result.get("finished_at"))
    duration = round((finished - started).total_seconds(), 1) if started and finished else None

    agent_result = result.get("agent_result") or {}
    cost = _num(agent_result.get("cost_usd"))
    model_name = str(((result.get("config") or {}).get("agent") or {}).get("model_name") or "")
    if model_name.startswith("openrouter/"):
        provider_cost = provider_cost_from_trajectory(trial)
        if provider_cost is not None:
            cost = provider_cost
    tokens = {
        "input": int(_num(agent_result.get("n_input_tokens")) or 0),
        "cached": int(_num(agent_result.get("n_cache_tokens")) or 0),
        "output": int(_num(agent_result.get("n_output_tokens")) or 0),
    }

    app = find_app_artifact(trial)
    if original is None or not original.is_dir():
        patch = "[patch unavailable: original task payload not found]\n"
    elif app is None:
        patch = "[patch unavailable: /app artifact was not collected for this attempt]\n"
    else:
        patch = unified_patch(original, app)
    patch = truncate_bytes(redact(patch), PATCH_LIMIT,
                           "[... patch truncated: {omitted} more bytes omitted (limit 300 KB) ...]")

    return {
        "trial": trial.name,
        "reward": reward,
        "score": round(score, 2),
        "exception": redact(exception) if exception else None,
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "duration_sec": duration,
        "cost_usd": round(cost, 6) if cost is not None else None,
        "tokens": tokens,
        "patch": patch,
        "verifier_tail": redact(verifier_tail(trial)),
        "agent_tail": redact(agent_tail(trial)),
    }


def find_trials(job_dir: Path) -> list[Path]:
    if not job_dir.is_dir():
        return []
    trials = [p for p in job_dir.iterdir()
              if p.is_dir() and (p / "result.json").is_file()]
    return sorted(trials, key=lambda p: ((read_json(p / "result.json", {}) or {}).get("started_at")
                                         or "", p.name))


def failure_attempt(message: str, log: Path | None) -> dict[str, Any]:
    return {
        "trial": None, "reward": 0, "score": 0.0, "exception": redact(message)[:500],
        "started_at": None, "finished_at": None, "duration_sec": None, "cost_usd": None,
        "tokens": {"input": 0, "cached": 0, "output": 0}, "patch": "",
        "verifier_tail": "",
        "agent_tail": redact(read_tail(log, TAIL_LINES)) if log and log.is_file() else "",
    }


def collect(job_dir: Path, task_dir: Path | None, *, task_id: str, agent: str, model: str,
            error: str | None = None) -> dict[str, Any]:
    original = task_dir / "environment" / "task_payload" if task_dir else None
    attempts = [parse_attempt(t, original) for t in find_trials(job_dir)]
    if not attempts:
        log = job_dir.parent / "harbor.log" if job_dir else None
        if log is None or not log.is_file():
            log = job_dir / "harbor.log" if job_dir else None
        exit_file = job_dir.parent / "harbor-exit-code"
        code = exit_file.read_text().strip() if exit_file.is_file() else "?"
        message = error or f"HarborError: harbor exited with code {code} and produced no trial results"
        attempts = [failure_attempt(message, log)]
    best = max((a["score"] for a in attempts), default=0.0)
    return {
        "task_id": task_id, "agent": agent, "model": model, "attempts": attempts,
        "best_score": best, "passed": any(a["reward"] == 1 for a in attempts),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--task-dir", type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--agent", default=os.environ.get("INPUT_AGENT", ""))
    parser.add_argument("--model", default=os.environ.get("INPUT_MODEL", ""))
    parser.add_argument("--error", default=os.environ.get("COLLECT_ERROR") or None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    fetch_outcome = os.environ.get("FETCH_OUTCOME", "")
    if args.error is None and fetch_outcome and fetch_outcome != "success":
        args.error = f"FetchError: the task bundle could not be fetched ({fetch_outcome}); see the job log"
    result = collect(args.job_dir, args.task_dir, task_id=args.task_id, agent=args.agent,
                     model=args.model, error=args.error)
    write_json(args.out, result)
    best = result["best_score"]
    print(f"{args.task_id}: passed={result['passed']} best_score={best} "
          f"attempts={len(result['attempts'])}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### {args.task_id}\n\n| attempt | reward | score | exception |\n|---|---|---|---|\n")
            for i, a in enumerate(result["attempts"], 1):
                exc = (a["exception"] or "").replace("|", "\\|")
                handle.write(f"| {i} | {a['reward']} | {a['score']} | {exc} |\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
