import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from common import InputError, kwargs_to_ak, parse_agent_kwargs  # noqa: E402
from plan_matrix import plan_evaluate  # noqa: E402

BASE = {
    "request_id": "ev-20261014-a1b2c3", "tasks": "patch-cwe94-0012,patch-cwe22-0003",
    "agent": "terminus-2", "model": "anthropic/claude-opus-4-8",
    "agent_kwargs": '{"reasoning_effort":"medium","max_turns":100}',
    "n_attempts": "2", "max_parallel": "4", "dataset": "xluobd/SecProbe-353",
}


def ev(**overrides):
    values = dict(BASE)
    values.update(overrides)
    return plan_evaluate(values)


class EvaluatePlanTests(unittest.TestCase):
    def test_valid_request(self):
        plan = ev()
        self.assertEqual(plan["tasks"], ["patch-cwe94-0012", "patch-cwe22-0003"])
        self.assertEqual(plan["matrix"]["include"][0], {"task": "patch-cwe94-0012",
                                                        "slug": "patch-cwe94-0012"})
        self.assertEqual(plan["n_attempts"], 2)
        self.assertEqual(plan["ak"], ["reasoning_effort=medium", "max_turns=100"])

    def test_generated_reference_and_dedupe(self):
        plan = ev(tasks="generated:gen-20261014-xxxx, patch-a ,patch-a")
        self.assertEqual(plan["tasks"], ["generated:gen-20261014-xxxx", "patch-a"])
        self.assertEqual(plan["matrix"]["include"][0]["slug"], "generated-gen-20261014-xxxx")

    def test_rejects_injection_in_task(self):
        for bad in ("patch;rm -rf /", "Patch-UPPER", "a/../b", "$(id)", "generated:../x",
                    "generated:", "x" * 101):
            with self.subTest(bad=bad), self.assertRaises(InputError):
                ev(tasks=bad)

    def test_task_limit(self):
        ev(tasks=",".join(f"t-{i}" for i in range(64)))
        with self.assertRaises(InputError):
            ev(tasks=",".join(f"t-{i}" for i in range(65)))
        with self.assertRaises(InputError):
            ev(tasks=" , ")

    def test_agent_allowlist(self):
        for agent in ("mini-swe-agent", "terminus-2", "claude-code", "codex", "gemini-cli",
                      "openhands", "aider", "goose", "opencode", "qwen-coder", "oracle"):
            ev(agent=agent)
        with self.assertRaises(InputError):
            ev(agent="terminus-2; curl evil")
        with self.assertRaises(InputError):
            ev(agent="my.module:Agent")

    def test_model_regex(self):
        ev(model="openrouter/z-ai/glm-5.3@preview:free")
        for bad in ("gpt 5", "a`b`", "x\ny", "$(whoami)", "m'o"):
            with self.subTest(bad=bad), self.assertRaises(InputError):
                ev(model=bad)
        with self.assertRaises(InputError):
            ev(model="")
        self.assertEqual(ev(agent="oracle", model="")["model"], "")

    def test_agent_kwargs(self):
        self.assertEqual(parse_agent_kwargs(""), {})
        self.assertEqual(parse_agent_kwargs('{"a": true, "b_2": 1.5, "c": "x"}'),
                         {"a": True, "b_2": 1.5, "c": "x"})
        for bad in ('[1]', '{"A": 1}', '{"a-b": 1}', '{"a": [1]}', '{"a": {"b": 1}}',
                    '{"a": null}', '{"a": "x\\ny"}', 'not json', '{"1a": 1}'):
            with self.subTest(bad=bad), self.assertRaises(InputError):
                parse_agent_kwargs(bad)

    def test_kwargs_encoding_preserves_strings(self):
        self.assertEqual(kwargs_to_ak({"a": "123", "b": "true", "c": "plain", "d": False}),
                         ['a="123"', 'b="true"', "c=plain", "d=false"])

    def test_numeric_bounds(self):
        for field, bad in (("n_attempts", "0"), ("n_attempts", "6"), ("n_attempts", "x"),
                           ("max_parallel", "0"), ("max_parallel", "65")):
            with self.subTest(field=field, bad=bad), self.assertRaises(InputError):
                ev(**{field: bad})

    def test_request_id_and_dataset(self):
        for bad in ("", "../x", "a b", "-lead", "x" * 81):
            with self.subTest(bad=bad), self.assertRaises(InputError):
                ev(request_id=bad)
        with self.assertRaises(InputError):
            ev(dataset="not-a-dataset")
        with self.assertRaises(InputError):
            ev(dataset="a/../b")

    def test_matrix_is_single_line_json(self):
        text = json.dumps(ev()["matrix"], separators=(",", ":"))
        self.assertNotIn("\n", text)


class GeneratedRepoTests(unittest.TestCase):
    def test_default_and_valid(self):
        self.assertEqual(ev()["generated_repo"], "Xiaonan-debug/secprobe-generated")
        self.assertEqual(ev(generated_repo="lab-x/secprobe-generated.v2")["generated_repo"],
                         "lab-x/secprobe-generated.v2")

    def test_rejects_bad_values(self):
        for bad in ("noslash", "a/b/c", "a/..", "../x", "a b/c", "a/b?x=1", "a/b#x", "a/b\nc",
                    "https://evil/x", "a_b/c", "a/$(id)"):
            with self.subTest(bad=bad), self.assertRaises(InputError):
                ev(generated_repo=bad)


if __name__ == "__main__":
    unittest.main()
