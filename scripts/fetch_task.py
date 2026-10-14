#!/usr/bin/env python3
"""Fetch one Harbor task into <dest>/<slug>/ (the directory holding task.toml).

Sources:
  * catalogue task  -> https://huggingface.co/datasets/<dataset>/resolve/main/tasks/<id>.tar.gz
  * generated:<rid> -> https://raw.githubusercontent.com/<generated_repo>/main/generated/<rid>/bundle.tar.gz
                       (tasks generated on the SecProbe website, published by the SecProbe team)
  * --local-root DIR (testing) -> DIR/<id>/harbor_task or DIR/<id> (directory copy), and for
                       generated:<rid> DIR/generated/<rid>/bundle.tar.gz

Prints the absolute task directory on stdout (last line) and writes it to the
`task_dir` step output when running inside GitHub Actions.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (DEFAULT_GENERATED_REPO, GENERATED_PREFIX, InputError,  # noqa: E402
                    set_output, task_slug, validate_dataset, validate_generated_repo,
                    validate_task_ref)

MAX_ARCHIVE_BYTES = 2 * 1024**3
HF_HOSTS = ("huggingface.co", "hf.co")


class _StripAuthOnForeignRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the HF token to a non-HF host after a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            host = (urllib.parse.urlparse(newurl).hostname or "").lower()
            if not any(host == h or host.endswith("." + h) for h in HF_HOSTS):
                new.remove_header("Authorization")
        return new


def download(url: str, target: Path, token: str | None, attempts: int = 4,
             not_found_hint: str = "") -> None:
    opener = urllib.request.build_opener(_StripAuthOnForeignRedirect())
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers={"User-Agent": "secprobe-runner/1"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with opener.open(request, timeout=120) as response, open(target, "wb") as out:
                total = 0
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise RuntimeError("archive exceeds 2 GiB limit")
                    out.write(chunk)
            return
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (401, 403, 404):
                hint = (" (private dataset? add an HF_TOKEN secret)" if exc.code in (401, 403)
                        else not_found_hint)
                raise RuntimeError(f"download failed: HTTP {exc.code} for {url}{hint}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
        time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"download failed after {attempts} attempts: {last}")


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(destination, filter="data")
            return
        base = destination.resolve()
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if not str(target).startswith(str(base) + os.sep) and target != base:
                raise RuntimeError(f"unsafe path in archive: {member.name}")
            if member.issym() or member.islnk() or member.isdev():
                raise RuntimeError(f"links/devices not allowed in archive: {member.name}")
        tar.extractall(destination)


def find_task_root(directory: Path) -> Path:
    if (directory / "task.toml").is_file():
        return directory
    candidates = sorted({p.parent for p in directory.glob("*/task.toml")}
                        | {p.parent for p in directory.glob("*/harbor_task/task.toml")})
    if len(candidates) != 1:
        raise RuntimeError(f"expected exactly one task.toml in archive, found {len(candidates)}")
    return candidates[0]


def generated_bundle_url(generated_repo: str, rid: str) -> str:
    repo = validate_generated_repo(generated_repo)
    return (f"https://raw.githubusercontent.com/{repo}/main/generated/"
            f"{urllib.parse.quote(rid)}/bundle.tar.gz")


def fetch(ref: str, dest: Path, *, dataset: str, local_root: Path | None, token: str | None,
          generated_repo: str = DEFAULT_GENERATED_REPO) -> Path:
    ref = validate_task_ref(ref)
    slug = task_slug(ref)
    final = dest / slug
    if final.exists():
        shutil.rmtree(final)
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest, prefix=".fetch-") as tmp_name:
        tmp = Path(tmp_name)
        rid = ref[len(GENERATED_PREFIX):] if ref.startswith(GENERATED_PREFIX) else None
        if local_root is not None and rid is None:
            source = local_root / ref
            if (source / "harbor_task" / "task.toml").is_file():
                source = source / "harbor_task"
            if not (source / "task.toml").is_file():
                raise RuntimeError(f"no task.toml under {source}")
            shutil.copytree(source, tmp / "extract" / ref, symlinks=False)
        else:
            archive = tmp / "bundle.tar.gz"
            if rid is not None and local_root is not None:
                source = local_root / "generated" / rid / "bundle.tar.gz"
                if not source.is_file():
                    raise RuntimeError(f"no generated bundle at {source}")
                shutil.copyfile(source, archive)
            elif rid is not None:
                url = generated_bundle_url(generated_repo, rid)
                print(f"Downloading {url}", file=sys.stderr)
                # Public repository: never send the HF token (or any other credential) there.
                download(url, archive, None,
                         not_found_hint=" (was the generation successful and the bundle <= 90 MB?)")
            else:
                dataset = validate_dataset(dataset)
                url = (f"https://huggingface.co/datasets/{dataset}/resolve/main/tasks/"
                       f"{urllib.parse.quote(ref)}.tar.gz")
                print(f"Downloading {url}", file=sys.stderr)
                download(url, archive, token)
            safe_extract(archive, tmp / "extract")
        root = find_task_root(tmp / "extract")
        shutil.move(str(root), str(final))
    for script in ("tests/test.sh", "solution/solve.sh"):
        path = final / script
        if path.is_file():
            path.chmod(0o755)
    return final.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", help="task id or generated:<request_id>")
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--dataset", default=os.environ.get("INPUT_DATASET") or "xluobd/SecProbe-353")
    parser.add_argument("--local-root", type=Path, help="copy from a local task pool (testing)")
    parser.add_argument("--generated-repo",
                        default=os.environ.get("INPUT_GENERATED_REPO") or DEFAULT_GENERATED_REPO,
                        help="public repo with generated/<rid>/bundle.tar.gz on main")
    args = parser.parse_args()
    try:
        task_dir = fetch(args.task, args.dest, dataset=args.dataset, local_root=args.local_root,
                         token=os.environ.get("HF_TOKEN") or None,
                         generated_repo=args.generated_repo)
    except (InputError, RuntimeError, tarfile.TarError) as exc:
        print(f"::error title=Task fetch failed::{exc}")
        return 1
    set_output("task_dir", str(task_dir))
    print(task_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
