import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from aggregate import aggregate  # noqa: E402
from publish import upsert_index  # noqa: E402

INPUTS = {"request_id": "ev-1", "tasks": "patch-a,patch-b,generated:gen-9", "agent": "terminus-2",
          "model": "openai/gpt-5", "agent_kwargs": "{}", "n_attempts": "2", "max_parallel": "4",
          "dataset": "xluobd/SecProbe-353"}


def attempt(reward, score, cost=None, exception=None):
    return {"reward": reward, "score": score, "exception": exception, "started_at": None,
            "finished_at": None, "duration_sec": 1, "cost_usd": cost,
            "tokens": {"input": 0, "cached": 0, "output": 0}, "patch": "", "verifier_tail": "",
            "agent_tail": ""}


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.results = self.tmp / "results"
        self.results.mkdir()
        (self.results / "patch-a.json").write_text(json.dumps({
            "task_id": "patch-a", "agent": "terminus-2", "model": "openai/gpt-5",
            "attempts": [attempt(0, 40.0, 0.5), attempt(1, 100.0, 0.25)],
            "best_score": 100.0, "passed": True}))
        (self.results / "generated-gen-9.json").write_text(json.dumps({
            "task_id": "generated:gen-9", "agent": "terminus-2", "model": "openai/gpt-5",
            "attempts": [attempt(0, 60.0, None, "AgentTimeoutError: slow")],
            "best_score": 60.0, "passed": False}))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_summary(self):
        out = self.tmp / "staging"
        summary = aggregate(INPUTS, self.results, out, plan_result="success",
                            run_result="failure", started_at="2026-10-14T00:00:00Z")
        base = out / "results" / "ev-1"
        self.assertEqual(json.loads((base / "summary.json").read_text()), summary)
        for key in ("request_id", "run_id", "run_url", "agent", "model", "status", "finished_at",
                    "n_tasks", "n_passed", "pass_rate", "mean_score", "total_cost_usd", "tasks"):
            self.assertIn(key, summary)
        self.assertEqual(summary["status"], "partial")  # patch-b produced no result
        self.assertEqual((summary["n_tasks"], summary["n_passed"]), (3, 1))
        self.assertAlmostEqual(summary["pass_rate"], 0.3333)
        self.assertAlmostEqual(summary["mean_score"], round((100 + 0 + 60) / 3, 2))
        self.assertAlmostEqual(summary["total_cost_usd"], 0.75)
        rows = {r["task_id"]: r for r in summary["tasks"]}
        self.assertEqual(rows["generated:gen-9"]["exception"], "AgentTimeoutError: slow")
        self.assertEqual(rows["generated:gen-9"]["file"], "tasks/generated-gen-9.json")
        self.assertTrue(rows["patch-b"]["exception"].startswith("MissingResult"))
        self.assertIsNone(rows["patch-a"]["exception"])
        self.assertTrue((base / "tasks" / "patch-b.json").is_file())
        request = json.loads((base / "request.json").read_text())
        self.assertEqual(request["tasks"], INPUTS["tasks"])
        self.assertIn("run_url", request)
        self.assertEqual(request["started_at"], "2026-10-14T00:00:00Z")

    def test_completed_and_cancelled(self):
        (self.results / "patch-b.json").write_text(json.dumps({
            "task_id": "patch-b", "agent": "a", "model": "m", "attempts": [attempt(0, 10.0)],
            "best_score": 10.0, "passed": False}))
        summary = aggregate(INPUTS, self.results, self.tmp / "s1", plan_result="success",
                            run_result="success", started_at="")
        self.assertEqual(summary["status"], "completed")
        (self.results / "patch-b.json").unlink()
        summary = aggregate(INPUTS, self.results, self.tmp / "s2", plan_result="success",
                            run_result="cancelled", started_at="")
        self.assertEqual(summary["status"], "cancelled")

    def test_invalid_inputs(self):
        bad = dict(INPUTS, agent="evil;rm")
        summary = aggregate(bad, self.results, self.tmp / "s3", plan_result="failure",
                            run_result="skipped", started_at="")
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["n_tasks"], 0)
        self.assertIn("Invalid inputs", summary["error"])
        self.assertIsNone(aggregate(dict(INPUTS, request_id="../../x"), self.results,
                                    self.tmp / "s4", plan_result="failure", run_result="",
                                    started_at=""))

    def test_index_upsert(self):
        index = self.tmp / "results" / "index.json"
        for i in range(5):
            upsert_index(index, {"request_id": f"r{i}", "started_at": f"2026-10-1{i}T00:00:00Z"}, 3)
        upsert_index(index, {"request_id": "r3", "started_at": "2026-10-13T00:00:00Z", "v": 2}, 3)
        data = json.loads(index.read_text())
        self.assertEqual([e["request_id"] for e in data], ["r4", "r3", "r2"])
        self.assertEqual(data[1]["v"], 2)


if __name__ == "__main__":
    unittest.main()
