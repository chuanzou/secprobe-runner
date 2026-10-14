import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import collect_result  # noqa: E402
from collect_result import collect, unified_patch  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "job_terminus"


class CollectFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.job = self.tmp / "harbor" / "patch-demo-0001"
        shutil.copytree(FIXTURE, self.job)
        # Original payload + downloaded /app artifact for the passing attempt.
        self.task = self.tmp / "tasks" / "patch-demo-0001"
        payload = self.task / "environment" / "task_payload"
        (payload / "src").mkdir(parents=True)
        (payload / "src" / "engine.py").write_text("def run(x):\n    return eval(x)\n")
        (payload / "README.md").write_text("demo\n")
        (payload / "logo.bin").write_bytes(b"\x00\x01\x02")
        app = self.job / "patch-demo-0001__passB" / "artifacts" / "app"
        shutil.copytree(payload, app)
        (app / "src" / "engine.py").write_text(
            "import ast\n\ndef run(x):\n    return ast.literal_eval(x)\n")
        (app / "src" / "new_helper.py").write_text("SAFE = True\n")
        (app / "README.md").unlink()
        (app / "logo.bin").write_bytes(b"\x00\x09")
        for junk in ("__pycache__/engine.cpython-311.pyc", "build/out.o", "node_modules/x/index.js",
                     ".pytest_cache/v/cache", "src/engine.pyc"):
            path = app / junk
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("junk")
        (app.parent / "manifest.json").write_text(json.dumps([
            {"source": "/logs/artifacts", "destination": "artifacts/logs/artifacts",
             "type": "directory", "status": "empty", "service": None},
            {"source": "/app", "destination": "artifacts/app", "type": "directory",
             "status": "ok", "service": None}]))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_task_result_shape(self):
        result = collect(self.job, self.task, task_id="patch-demo-0001", agent="terminus-2",
                         model="openai/gpt-5.6-sol")
        self.assertEqual(set(result), {"task_id", "agent", "model", "attempts", "best_score",
                                       "passed"})
        self.assertTrue(result["passed"])
        self.assertEqual(result["best_score"], 100.0)
        attempts = result["attempts"]
        self.assertEqual(len(attempts), 3)
        keys = {"reward", "score", "exception", "started_at", "finished_at", "duration_sec",
                "cost_usd", "tokens", "patch", "verifier_tail", "agent_tail"}
        for attempt in attempts:
            self.assertTrue(keys <= set(attempt))
            self.assertIn(attempt["reward"], (0, 1))
            self.assertEqual(set(attempt["tokens"]), {"input", "cached", "output"})

        by_trial = {a["trial"]: a for a in attempts}
        fail = by_trial["patch-demo-0001__failA"]
        self.assertEqual(fail["reward"], 0)
        self.assertAlmostEqual(fail["score"], 41.99, places=2)
        self.assertIsNone(fail["exception"])
        self.assertIn("grade: score=41.9923", fail["verifier_tail"])
        self.assertIn("Security: secure type policy", fail["verifier_tail"])
        self.assertIn("--- diagnostic.txt ---", fail["verifier_tail"])
        self.assertLessEqual(len(fail["verifier_tail"].splitlines()), 152)
        self.assertIn("/app artifact was not collected", fail["patch"])

        ok = by_trial["patch-demo-0001__passB"]
        self.assertEqual((ok["reward"], ok["score"]), (1, 100.0))
        self.assertEqual(ok["tokens"], {"input": 229031, "cached": 0, "output": 7261})
        self.assertAlmostEqual(ok["cost_usd"], 0.317051, places=5)
        self.assertAlmostEqual(ok["duration_sec"], 310.1, places=1)
        self.assertNotIn("\x1b[", ok["agent_tail"])
        self.assertTrue(ok["agent_tail"])
        self.assertLessEqual(len(ok["agent_tail"].splitlines()), 151)

        timeout = by_trial["patch-demo-0001__timeoutC"]
        self.assertTrue(timeout["exception"].startswith("AgentTimeoutError: Agent execution timed out"))
        # attempts sorted by started_at
        starts = [a["started_at"] for a in attempts]
        self.assertEqual(starts, sorted(starts))

    def test_patch_contents(self):
        result = collect(self.job, self.task, task_id="patch-demo-0001", agent="terminus-2",
                         model="m")
        patch = next(a for a in result["attempts"] if a["reward"] == 1)["patch"]
        self.assertIn("diff --git a/src/engine.py b/src/engine.py", patch)
        self.assertIn("-    return eval(x)", patch)
        self.assertIn("+    return ast.literal_eval(x)", patch)
        self.assertIn("+++ b/src/new_helper.py", patch)
        self.assertIn("--- /dev/null", patch)
        self.assertIn("deleted file mode", patch)
        self.assertIn("Binary files a/logo.bin and b/logo.bin differ", patch)
        for junk in ("__pycache__", "build/out.o", "node_modules", ".pytest_cache", "engine.pyc"):
            self.assertNotIn(junk, patch)

    def test_patch_truncation(self):
        original, modified = self.tmp / "o", self.tmp / "m"
        original.mkdir()
        modified.mkdir()
        (modified / "big.txt").write_text("".join(f"line {i} {'x' * 60}\n" for i in range(8000)))
        patch = unified_patch(original, modified)
        self.assertGreater(len(patch.encode()), collect_result.PATCH_LIMIT)
        from common import truncate_bytes
        cut = truncate_bytes(patch, collect_result.PATCH_LIMIT, "[... patch truncated: {omitted} more bytes ...]")
        self.assertLessEqual(len(cut.encode()), collect_result.PATCH_LIMIT + 100)
        self.assertIn("patch truncated", cut)

    def test_no_trials_gives_failure_attempt(self):
        jobs = self.tmp / "empty"
        (jobs / "job").mkdir(parents=True)
        (jobs / "harbor.log").write_text("Building environment...\nError: docker daemon down\n")
        (jobs / "harbor-exit-code").write_text("1\n")
        result = collect(jobs / "job", None, task_id="patch-x", agent="codex", model="openai/o")
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["attempts"]), 1)
        self.assertIn("HarborError: harbor exited with code 1", result["attempts"][0]["exception"])
        self.assertIn("docker daemon down", result["attempts"][0]["agent_tail"])
        fetch = collect(self.tmp / "missing" / "job", None, task_id="patch-x", agent="codex",
                        model="m", error="FetchError: boom")
        self.assertEqual(fetch["attempts"][0]["exception"], "FetchError: boom")

    def test_redaction(self):
        os.environ["OPENAI_API_KEY"] = "test-secret-value-1234567890"
        try:
            pane = self.job / "patch-demo-0001__passB" / "agent" / "terminus_2.pane"
            pane.write_text(pane.read_text() + "\nexport KEY=test-secret-value-1234567890\n"
                            "token sk-proj-abcdefghijklmnopqrstuvwxyz0123\n")
            result = collect(self.job, self.task, task_id="patch-demo-0001", agent="t", model="m")
            text = json.dumps(result)
            self.assertNotIn("test-secret-value-1234567890", text)
            self.assertNotIn("sk-proj-abcdefghijklmnopqrstuvwxyz0123", text)
        finally:
            del os.environ["OPENAI_API_KEY"]

    def test_openrouter_mini_swe_agent_provider_cost(self):
        trial = self.job / "patch-demo-0001__passB"
        data = json.loads((trial / "result.json").read_text())
        data["config"]["agent"]["model_name"] = "openrouter/z-ai/glm-5.3"
        data["agent_result"]["cost_usd"] = 0.0
        (trial / "result.json").write_text(json.dumps(data))
        (trial / "agent" / "mini-swe-agent.trajectory.json").write_text(json.dumps({"messages": [
            {"role": "user"},
            {"extra": {"response": {"usage": {"cost": 0.25}}}},
            {"extra": {"response": {"usage": {"cost": 0.5}}}},
        ]}))
        result = collect(self.job, self.task, task_id="patch-demo-0001", agent="mini-swe-agent",
                         model="openrouter/z-ai/glm-5.3")
        ok = next(a for a in result["attempts"] if a["trial"] == trial.name)
        self.assertAlmostEqual(ok["cost_usd"], 0.75)


if __name__ == "__main__":
    unittest.main()
