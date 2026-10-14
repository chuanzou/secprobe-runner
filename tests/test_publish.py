"""publish.py against a local bare repository standing in for GitHub."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from publish import publish  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        seed = self.tmp / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "README.md").write_text("runner\n")
        (seed / "scripts").mkdir()
        (seed / "scripts" / "x.py").write_text("print(1)\n")
        git(seed, "add", "-A")
        git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-q", "origin", "main")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clone(self, name):
        path = self.tmp / name
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.origin}", str(path)],
                       check=True)
        return path

    def stage(self, name, rid):
        staging = self.tmp / f"stage-{name}"
        (staging / "results" / rid).mkdir(parents=True)
        (staging / "results" / rid / "summary.json").write_text(json.dumps({"request_id": rid}))
        return staging

    def branch_files(self):
        check = self.clone(f"check-{len(list(self.tmp.iterdir()))}")
        git(check, "fetch", "-q", "origin", "secprobe-results")
        return git(check, "ls-tree", "-r", "--name-only", "FETCH_HEAD").split(), check

    def test_creates_orphan_branch_and_is_idempotent(self):
        repo = self.clone("a")
        entry = {"request_id": "r1", "started_at": "2026-10-14T00:00:00Z"}
        self.assertTrue(publish(repo, self.stage("a", "r1"), "results: r1",
                                [("results/index.json", entry)]))
        files, check = self.branch_files()
        self.assertEqual(sorted(files), ["results/index.json", "results/r1/summary.json"])
        # orphan: no shared history with main
        self.assertEqual(git(check, "rev-list", "--count", "FETCH_HEAD").strip(), "1")
        self.assertFalse(publish(repo, self.stage("a2", "r1"), "again",
                                 [("results/index.json", entry)]))
        self.assertEqual(git(repo, "status", "--porcelain"), "")  # main checkout untouched
        self.assertEqual(git(repo, "worktree", "list").count("\n"), 1)

    def test_concurrent_publishers(self):
        script = (
            "import sys, json; sys.path.insert(0, sys.argv[1]);"
            "from pathlib import Path; from publish import publish;"
            "rid=sys.argv[3]; publish(Path(sys.argv[2]), Path(sys.argv[4]), 'results: '+rid,"
            "[('results/index.json', {'request_id': rid, 'started_at': rid})])"
        )
        procs = []
        for i in range(6):
            repo = self.clone(f"c{i}")
            rid = f"2026-r{i}"
            procs.append(subprocess.Popen([sys.executable, "-c", script, str(SCRIPTS), str(repo),
                                           rid, str(self.stage(f"c{i}", rid))],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True))
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
        files, check = self.branch_files()
        for i in range(6):
            self.assertIn(f"results/2026-r{i}/summary.json", files)
        index = json.loads(git(check, "show", "FETCH_HEAD:results/index.json"))
        self.assertEqual(sorted(e["request_id"] for e in index), [f"2026-r{i}" for i in range(6)])
        self.assertEqual(index[0]["request_id"], "2026-r5")


if __name__ == "__main__":
    unittest.main()
