"""Shared stdlib-only helpers for the SecProbe runner scripts.

Everything that touches workflow inputs goes through the validators here so the
`plan` job and the per-task jobs apply exactly the same rules.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RESULTS_BRANCH = "secprobe-results"

# Harbor agents the website offers, plus `oracle` (runs the reference solution,
# costs nothing, useful to check that a runner works) and `nop`.
AGENTS = (
    "mini-swe-agent", "terminus-2", "claude-code", "codex", "gemini-cli", "openhands",
    "aider", "goose", "opencode", "qwen-coder", "oracle", "nop",
)
AGENTS_WITHOUT_MODEL = ("oracle", "nop")

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
TASK_ID_RE = re.compile(r"^[a-z0-9-]+$")
GENERATED_PREFIX = "generated:"
MODEL_RE = re.compile(r"^[A-Za-z0-9._/:@-]+$")
KWARG_KEY_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
DATASET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
# Public repository holding generated tasks (generated/<request_id>/bundle.tar.gz on main).
GENERATED_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
DEFAULT_GENERATED_REPO = "Xiaonan-debug/secprobe-generated"

MAX_TASKS = 64
MAX_TASK_ID_LEN = 100
MAX_MODEL_LEN = 200
MAX_KWARGS = 32
MAX_KWARG_STR = 1000

SECRET_NAMES = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "ZAI_API_KEY", "DASHSCOPE_API_KEY",
    "MISTRAL_API_KEY", "MINIMAX_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY",
    "XAI_API_KEY", "HF_TOKEN",
    # LiteLLM aliases populated from the secrets above by the workflow
    "TOGETHERAI_API_KEY", "FIREWORKS_AI_API_KEY", "GOOGLE_API_KEY", "GITHUB_TOKEN", "GH_TOKEN",
)


class InputError(ValueError):
    """A workflow input failed validation (message is safe to print)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Python < 3.11 cannot parse 7+ fractional digits; trim to microseconds.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def set_output(name: str, value: str) -> None:
    """Write a step output (GITHUB_OUTPUT) or print it when run locally."""
    target = os.environ.get("GITHUB_OUTPUT")
    if "\n" in value:
        raise ValueError("multi-line outputs are not supported")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        print(f"[output] {name}={value}")


# ---------------------------------------------------------------- validation

def validate_request_id(value: str) -> str:
    value = (value or "").strip()
    if not REQUEST_ID_RE.match(value):
        raise InputError("request_id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    return value


def validate_task_ref(value: str) -> str:
    value = value.strip()
    if value.startswith(GENERATED_PREFIX):
        rid = value[len(GENERATED_PREFIX):]
        if not REQUEST_ID_RE.match(rid):
            raise InputError(f"invalid generated task reference: {value[:120]!r}")
        return value
    if len(value) > MAX_TASK_ID_LEN or not TASK_ID_RE.match(value):
        raise InputError(f"invalid task id: {value[:120]!r} (expected ^[a-z0-9-]+$)")
    return value


def task_slug(ref: str) -> str:
    """Filesystem/artifact-safe name: `generated:<rid>` -> `generated-<rid>`."""
    if ref.startswith(GENERATED_PREFIX):
        return "generated-" + ref[len(GENERATED_PREFIX):]
    return ref


def parse_task_list(value: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[,\s]+", value or "") if p.strip()]
    if not parts:
        raise InputError("tasks must list at least one task id")
    seen: dict[str, None] = {}
    for part in parts:
        seen.setdefault(validate_task_ref(part), None)
    tasks = list(seen)
    if len(tasks) > MAX_TASKS:
        raise InputError(f"at most {MAX_TASKS} tasks per request (got {len(tasks)})")
    slugs = [task_slug(t) for t in tasks]
    if len(set(slugs)) != len(slugs):
        raise InputError("task references collide after normalisation")
    return tasks


def validate_agent(value: str) -> str:
    value = (value or "").strip()
    if value not in AGENTS:
        raise InputError(f"agent must be one of: {', '.join(AGENTS)}")
    return value


def validate_model(value: str, *, required: bool = True) -> str:
    value = (value or "").strip()
    if not value:
        if required:
            raise InputError("model is required")
        return ""
    if len(value) > MAX_MODEL_LEN or not MODEL_RE.match(value):
        raise InputError("model must match ^[A-Za-z0-9._/:@-]+$")
    return value


def validate_int(name: str, value: str | int, lo: int, hi: int) -> int:
    try:
        number = int(str(value).strip())
    except ValueError as exc:
        raise InputError(f"{name} must be an integer between {lo} and {hi}") from exc
    if not lo <= number <= hi:
        raise InputError(f"{name} must be between {lo} and {hi}")
    return number


def validate_dataset(value: str) -> str:
    value = (value or "").strip()
    if not DATASET_RE.match(value) or ".." in value:
        raise InputError("dataset must look like <owner>/<name>")
    return value


def validate_generated_repo(value: str) -> str:
    value = (value or "").strip()
    if not GENERATED_REPO_RE.fullmatch(value) or ".." in value or len(value) > 200:
        raise InputError("generated_repo must look like <owner>/<name>")
    return value


def _scalar_ok(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return value is not None
    if isinstance(value, int):
        return abs(value) < 10**15
    if isinstance(value, float):
        return value == value and abs(value) < 1e15  # not NaN / inf
    if isinstance(value, str):
        return len(value) <= MAX_KWARG_STR and not any(ch in value for ch in "\r\n\x00")
    return False


def parse_agent_kwargs(value: str) -> dict[str, Any]:
    text = (value or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError("agent_kwargs must be a JSON object") from exc
    if not isinstance(data, dict):
        raise InputError("agent_kwargs must be a JSON object")
    if len(data) > MAX_KWARGS:
        raise InputError(f"agent_kwargs may contain at most {MAX_KWARGS} keys")
    for key, item in data.items():
        if not KWARG_KEY_RE.match(key) or len(key) > 64:
            raise InputError(f"agent_kwargs key {key[:40]!r} must match ^[a-z_][a-z0-9_]*$")
        if not _scalar_ok(item):
            raise InputError(f"agent_kwargs[{key}] must be a string, number or boolean")
    return data


def kwargs_to_ak(kwargs: dict[str, Any]) -> list[str]:
    """Encode kwargs as Harbor `--ak key=value` values (Harbor JSON-decodes values)."""
    out = []
    for key, value in kwargs.items():
        if isinstance(value, str):
            # Harbor json.loads() the value first; quote strings that would otherwise
            # be re-interpreted (numbers, true/false, lists, ...).
            try:
                json.loads(value)
                encoded = json.dumps(value)
            except json.JSONDecodeError:
                encoded = value
        else:
            encoded = json.dumps(value)
        out.append(f"{key}={encoded}")
    return out


# ---------------------------------------------------------------- text helpers

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
SECRET_PATTERNS = [
    re.compile(r"sk-(?:ant-|proj-|or-v1-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"xai-[A-Za-z0-9]{30,}"),
]


def strip_ansi(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n")
    # Collapse carriage-return overwrites (progress bars) to the final segment.
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))


def redact(text: str, extra_values: list[str] | None = None) -> str:
    if not text:
        return text
    values = [os.environ.get(name, "") for name in SECRET_NAMES] + list(extra_values or [])
    for value in sorted({v for v in values if v and len(v) >= 8}, key=len, reverse=True):
        text = text.replace(value, "***")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("***", text)
    return text


MAX_LINE_CHARS = 1000


def tail_lines(text: str, n: int) -> str:
    lines = [line if len(line) <= MAX_LINE_CHARS
             else line[:MAX_LINE_CHARS] + f" [... {len(line) - MAX_LINE_CHARS} chars]"
             for line in text.rstrip("\n").split("\n")]
    if len(lines) <= n:
        return "\n".join(lines)
    return "\n".join([f"[... {len(lines) - n} earlier lines omitted ...]"] + lines[-n:])


def read_tail(path: Path, n_lines: int, max_bytes: int = 512_000) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        text = text.split("\n", 1)[-1]
    return tail_lines(strip_ansi(text), n_lines)


def truncate_bytes(text: str, limit: int, marker: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[:limit].decode("utf-8", errors="ignore")
    cut = cut.rsplit("\n", 1)[0] if "\n" in cut else cut
    omitted = len(raw) - len(cut.encode("utf-8"))
    return cut + "\n" + marker.format(omitted=omitted) + "\n"


def run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else ""


def run_id() -> int:
    try:
        return int(os.environ.get("GITHUB_RUN_ID", "0"))
    except ValueError:
        return 0
