import io
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_task  # noqa: E402
from common import InputError  # noqa: E402


def make_bundle(path: Path, top: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for name, text in ((f"{top}/task.toml", "[task]\n"), (f"{top}/tests/test.sh", "exit 0\n")):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


class FetchGeneratedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_url(self):
        self.assertEqual(fetch_task.generated_bundle_url("lab/secprobe-generated", "gen-1"),
                         "https://raw.githubusercontent.com/lab/secprobe-generated/main/"
                         "generated/gen-1/bundle.tar.gz")
        with self.assertRaises(InputError):
            fetch_task.generated_bundle_url("evil.com/x/y", "gen-1")

    def test_local_root_generated_bundle(self):
        make_bundle(self.tmp / "pool" / "generated" / "gen-1" / "bundle.tar.gz", "gen-1")
        task = fetch_task.fetch("generated:gen-1", self.tmp / "dest", dataset="x/y",
                                local_root=self.tmp / "pool", token=None)
        self.assertEqual(task.name, "generated-gen-1")
        self.assertTrue((task / "task.toml").is_file())
        self.assertTrue((task / "tests" / "test.sh").stat().st_mode & 0o100)

    def test_download_uses_generated_repo_without_token(self):
        calls = []

        def fake_download(url, target, token, attempts=4, not_found_hint=""):
            calls.append((url, token))
            make_bundle(Path(target), "gen-2")

        with mock.patch.object(fetch_task, "download", fake_download):
            task = fetch_task.fetch("generated:gen-2", self.tmp / "dest", dataset="x/y",
                                    local_root=None, token="hf_secret_token_value_123456",
                                    generated_repo="lab/secprobe-generated")
        self.assertEqual(calls, [("https://raw.githubusercontent.com/lab/secprobe-generated/main/"
                                  "generated/gen-2/bundle.tar.gz", None)])
        self.assertTrue((task / "task.toml").is_file())

    def test_invalid_refs(self):
        for ref in ("generated:../x", "generated:", "a;b"):
            with self.subTest(ref=ref), self.assertRaises(InputError):
                fetch_task.fetch(ref, self.tmp / "dest", dataset="x/y", local_root=None, token=None)


if __name__ == "__main__":
    unittest.main()
