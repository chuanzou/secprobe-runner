# SecProbe runner

This repository runs [SecProbe](https://github.com/secprobe) benchmark jobs **in your own
GitHub Actions**, using **your own API keys** stored as repository secrets. The SecProbe website
creates it for you from the `secprobe-runner` template, dispatches the workflows below and reads
the results back from the `secprobe-results` branch. Nothing is sent to SecProbe's servers: the
website talks to the GitHub API from your browser with your GitHub login.

| Workflow | What it does |
|---|---|
| `evaluate.yml` | Runs a [Harbor](https://pypi.org/project/harbor/) agent (`harbor==0.20.0`) on up to 64 SecProbe tasks, one GitHub job per task, and publishes rewards, scores, costs, tokens, the agent's patch and log tails. |

This runner only **evaluates**. Generating new tasks is hosted by the SecProbe team: you request a
generation on the website (with your own OpenAI key, sealed in your browser so only the hosted
generator can read it), and finished tasks are published to the public repository
`Xiaonan-debug/secprobe-generated`. Any of them can be evaluated here as `generated:<request_id>`.

## One-time setup

The website normally does all of this automatically after you sign in with GitHub:

1. Create the repo from the template (`Use this template` → name it `secprobe-runner`). Public or
   private both work — see the safety notes below.
2. Add the provider keys you want to use under **Settings → Secrets and variables → Actions**.
   All are optional; add only what you use:

   | Secret | Used for |
   |---|---|
   | `OPENAI_API_KEY` | OpenAI models, `codex` |
   | `ANTHROPIC_API_KEY` | Anthropic models, `claude-code` |
   | `GEMINI_API_KEY` | Google Gemini models, `gemini-cli` |
   | `OPENROUTER_API_KEY` | `openrouter/...` models |
   | `DEEPSEEK_API_KEY`, `MOONSHOT_API_KEY`, `ZAI_API_KEY`, `DASHSCOPE_API_KEY`, `MISTRAL_API_KEY`, `MINIMAX_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, `XAI_API_KEY` | the corresponding LiteLLM providers (`TOGETHER_API_KEY` is also exported as `TOGETHERAI_API_KEY`, `FIREWORKS_API_KEY` as `FIREWORKS_AI_API_KEY`) |
   | `HF_TOKEN` | only if the task dataset on Hugging Face is private/gated |

3. Make sure Actions are enabled (**Settings → Actions → General**; *Read and write permissions*
   is not required — the workflows request `contents: write` themselves).

## Manual usage

```bash
# Evaluate two tasks with Terminus-2, 1 attempt each
gh workflow run evaluate.yml -R <you>/secprobe-runner \
  -f request_id=ev-20261014-a1b2c3 \
  -f tasks=patch-cwe94-0012,patch-cwe22-0003 \
  -f agent=terminus-2 \
  -f model=anthropic/claude-opus-4-8 \
  -f agent_kwargs='{"max_turns":100}' \
  -f n_attempts=1 -f max_parallel=4

# Check that the runner works without spending tokens (the oracle applies the reference fix)
gh workflow run evaluate.yml -R <you>/secprobe-runner -f request_id=ev-oracle-1 \
  -f tasks=patch-cwe94-0012 -f agent=oracle

# Evaluate a task generated on the SecProbe website
gh workflow run evaluate.yml -R <you>/secprobe-runner -f request_id=ev-20261014-0002 \
  -f tasks=generated:gen-20261014-1a2b3c4d -f agent=mini-swe-agent -f model=openai/gpt-5.3
```

### `evaluate.yml` inputs

| Input | Rules |
|---|---|
| `request_id` | `^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$`, unique per request |
| `tasks` | comma-separated, ≤ 64; each `^[a-z0-9-]+$` or `generated:<request_id>` |
| `agent` | `mini-swe-agent`, `terminus-2`, `claude-code`, `codex`, `gemini-cli`, `openhands`, `aider`, `goose`, `opencode`, `qwen-coder` (plus `oracle`/`nop` for smoke tests) |
| `model` | `^[A-Za-z0-9._/:@-]+$`, LiteLLM style `provider/model` (optional for `oracle`/`nop`) |
| `agent_kwargs` | flat JSON object of strings/numbers/booleans, keys `^[a-z_][a-z0-9_]*$`; each becomes `--ak key=value` |
| `n_attempts` | 1–5 (attempts of one task run concurrently inside its job) |
| `max_parallel` | 1–64 concurrent task jobs |
| `dataset` | Hugging Face dataset with `tasks/<id>.tar.gz` (default `xluobd/SecProbe-353`) |
| `generated_repo` | public GitHub repo `^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$` holding generated tasks (default `Xiaonan-debug/secprobe-generated`); `generated:<id>` is downloaded from `https://raw.githubusercontent.com/<generated_repo>/main/generated/<id>/bundle.tar.gz` |

Jobs: `plan` validates the inputs and builds the matrix → `task · <task_id>` (one per task:
fetch the bundle, `harbor run -p <task> -a <agent> -m <model> -k <n> --artifact /app -y`,
collect the result) → `publish` (always runs, aggregates and commits). Each task job has a
240-minute limit (agent 2 h + build 20 min + verifier 10 min + setup).

## Where results go

Everything is committed to the orphan branch **`secprobe-results`** (created on first use) by
`scripts/publish.py`, which retries on concurrent pushes:

```
results/index.json                         summaries of all evaluations, newest first (≤ 500)
results/<request_id>/request.json          inputs + run id/url + start time
results/<request_id>/summary.json          pass rate, mean score, total cost, per-task rows
results/<request_id>/tasks/<task>.json     per-attempt reward, score, exception, timings,
                                           cost, tokens, patch (≤ 300 KB), verifier/agent log tails
```

For `generated:<id>` task references the per-task file is `tasks/generated-<id>.json` (`:` is not
allowed in artifact names); `summary.json` lists each task's `file`. Summary `status` is
`completed`, `partial` (some task jobs produced no result), `cancelled` or `failed` (invalid
inputs; see `error`).

Workflow artifacts (30 days): `harbor-<task>` — the full Harbor job directory (trajectories,
logs, the agent's `/app`).

## Cost and safety notes

* **You pay** for every model call made with your keys, and for Actions minutes beyond your free
  allowance on private repos. Evaluations stop at the task's 2-hour agent timeout. Start with one task.
* **Model-written code runs on GitHub's ephemeral runner.** Agents execute code that a model
  wrote. Each job gets a fresh VM that is destroyed afterwards; agents run inside
  Docker containers on it. Do not add secrets you would not hand to such code: the evaluate
  step that runs Harbor has your provider keys in its environment.
* **Public repositories expose logs and results publicly.** Anyone can read workflow logs, the
  `secprobe-results` branch (patches, log tails) and download artifacts. GitHub masks secret
  values in logs and the collectors redact them (and common key formats) before anything is
  committed, but make the repo private if your runs or prompts are confidential.
* Inputs are validated (`scripts/plan_matrix.py`) and passed to scripts only via environment
  variables, never interpolated into shell commands.
* Remove a key any time under **Settings → Secrets**; delete the repo to remove everything.

## Development

```bash
python3 -m unittest discover -s tests      # stdlib only
actionlint .github/workflows/*.yml         # optional
```

`scripts/fetch_task.py --local-root <pool>` copies a task from a local folder instead of
Hugging Face (and reads `generated:<id>` from `<pool>/generated/<id>/bundle.tar.gz`), which lets
you run the whole evaluate path locally:

```bash
python scripts/fetch_task.py patch-cwe94-0012 --dest work/tasks --local-root /path/to/SecProbe_tasks_353
INPUT_AGENT=oracle python scripts/run_harbor.py --task-dir work/tasks/patch-cwe94-0012 \
  --jobs-dir work/harbor --job-name patch-cwe94-0012
python scripts/collect_result.py --job-dir work/harbor/patch-cwe94-0012 \
  --task-dir work/tasks/patch-cwe94-0012 --task-id patch-cwe94-0012 --agent oracle --out work/r.json
```
