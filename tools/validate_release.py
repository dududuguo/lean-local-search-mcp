#!/usr/bin/env python3

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = Path(__file__).resolve().with_name("lean_local_search_mcp.py")
RELEASE_URL = "https://github.com/dududuguo/HighDimProb/archive/refs/tags/alpha-0.1.tar.gz"
RELEASE_SHA256 = "f837fbf000516f2494df2a6489b6ee7699c0961da33e82385d0922aa1678e533"
RELEASE_ROOT = "HighDimProb-alpha-0.1"
ARCHIVE_NAME = f"{RELEASE_ROOT}.tar.gz"
DEFAULT_CACHE_DIR = REPO_ROOT / ".lean-local-search" / "fixtures" / "highdimprob-alpha-0.1"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_archive(url, destination):
    if shutil.which("curl") is None:
        raise RuntimeError("curl is required to download the pinned release fixture")
    subprocess.run(
        ["curl", "--fail", "--show-error", "--location", url, "--output", str(destination)],
        check=True,
    )


def lock_windows_file(lock_file, locking, nonblocking_mode, timeout=300, retry_interval=0.1):
    deadline = time.monotonic() + timeout
    while True:
        lock_file.seek(0)
        try:
            locking(lock_file.fileno(), nonblocking_mode, 1)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting {timeout}s for fixture cache lock") from exc
            time.sleep(retry_interval)


@contextmanager
def cache_lock(cache_dir):
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (cache_dir / ".fixture.lock").open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_windows_file(lock_file, msvcrt.locking, msvcrt.LK_NBLCK)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        elif locked:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def safe_archive_members(archive):
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in member.name
            or member.issym()
            or member.islnk()
            or not (member.isfile() or member.isdir())
        ):
            raise ValueError(f"unsafe archive member: {member.name}")
        if not path.parts or path.parts[0] != RELEASE_ROOT:
            raise ValueError(f"unexpected archive root: {member.name}")
    return members


def archive_manifest(archive_path):
    manifest = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        members = safe_archive_members(archive)
        for member in members:
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            digest = hashlib.sha256(stream.read()).hexdigest()
            relative = PurePosixPath(member.name).relative_to(RELEASE_ROOT).as_posix()
            manifest[relative] = digest
    return manifest


def source_manifest(source_dir):
    manifest = {}
    for path in sorted(Path(source_dir).rglob("*")):
        if path.name == ".release-sha256":
            continue
        if path.is_symlink():
            manifest[path.relative_to(source_dir).as_posix()] = "symlink"
        elif path.is_file():
            manifest[path.relative_to(source_dir).as_posix()] = sha256_file(path)
        elif not path.is_dir():
            raise ValueError(f"unsafe cached fixture entry: {path}")
    return manifest


def prepare_fixture(cache_dir=DEFAULT_CACHE_DIR, url=RELEASE_URL, expected_sha256=RELEASE_SHA256, downloader=download_archive):
    cache_dir = Path(cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    with cache_lock(cache_dir):
        return _prepare_fixture_locked(cache_dir, url, expected_sha256, downloader)


def _prepare_fixture_locked(cache_dir, url, expected_sha256, downloader):
    archive_path = cache_dir / ARCHIVE_NAME
    source_dir = cache_dir / RELEASE_ROOT
    marker = source_dir / ".release-sha256"

    archive_valid = archive_path.is_file() and sha256_file(archive_path) == expected_sha256
    downloaded = False
    if not archive_valid:
        fd, temp_name = tempfile.mkstemp(prefix=f".{ARCHIVE_NAME}.", dir=cache_dir)
        os.close(fd)
        temp_archive = Path(temp_name)
        try:
            downloader(url, temp_archive)
            actual = sha256_file(temp_archive)
            if actual != expected_sha256:
                raise ValueError(f"release archive SHA-256 mismatch: expected {expected_sha256}, got {actual}")
            os.replace(temp_archive, archive_path)
            downloaded = True
        finally:
            temp_archive.unlink(missing_ok=True)

    expected_manifest = archive_manifest(archive_path)
    source_valid = (
        source_dir.is_dir()
        and marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == expected_sha256
        and source_manifest(source_dir) == expected_manifest
    )
    if source_valid:
        return source_dir, "downloaded" if downloaded else "reused"

    extract_dir = Path(tempfile.mkdtemp(prefix=".extract-", dir=cache_dir))
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = safe_archive_members(archive)
            archive.extractall(extract_dir, members=members)
        extracted_root = extract_dir / RELEASE_ROOT
        if not extracted_root.is_dir():
            raise ValueError(f"archive did not contain {RELEASE_ROOT}")
        (extracted_root / ".release-sha256").write_text(expected_sha256 + "\n", encoding="utf-8")
        backup_dir = cache_dir / f".{RELEASE_ROOT}.previous"
        shutil.rmtree(backup_dir, ignore_errors=True)
        if source_dir.exists():
            os.replace(source_dir, backup_dir)
        try:
            os.replace(extracted_root, source_dir)
        except Exception:
            if backup_dir.exists() and not source_dir.exists():
                os.replace(backup_dir, source_dir)
            raise
        shutil.rmtree(backup_dir, ignore_errors=True)
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
    return source_dir, "downloaded" if downloaded else "repaired"


def require(condition, invariant, result):
    if not condition:
        raise AssertionError(f"{invariant} failed: {json.dumps(result, ensure_ascii=False, default=str)}")


def load_server():
    spec = importlib.util.spec_from_file_location("lean_local_search_release_validation", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_release(repo, keep_index=False, index_dir=None):
    repo = Path(repo).resolve()
    owns_index_dir = index_dir is None
    index_dir = Path(tempfile.mkdtemp(prefix="lean-local-search-release-index-")) if owns_index_dir else Path(index_dir).resolve()
    index_dir.mkdir(parents=True, exist_ok=True)
    previous_index_root = os.environ.get("LEAN_SEARCH_INDEX_ROOT")
    os.environ["LEAN_SEARCH_INDEX_ROOT"] = str(index_dir)
    try:
        mod = load_server()
        full = mod.index_repository({"mode": "full"}, repo)
        require(full["indexed_files"] == 342, "indexed file baseline", full)
        require(full["declarations"] == 2560, "declaration baseline", full)
        require(full["imports"] == 824, "import baseline", full)

        incremental = mod.index_repository({"mode": "incremental"}, repo)
        require(incremental["changed_files"] == 0, "incremental no-op", incremental)
        require(incremental["skipped_files"] == 342, "incremental skipped files", incremental)

        graph = mod.search_graph({"repo_path": str(repo), "query": "markov inequality", "limit": 10}, repo)
        require(any(r["qualified_name"] == "HighDimProb.markov_inequality" for r in graph["results"]), "graph search Markov theorem", graph)

        theorems = mod.search_theorems({"repo_path": str(repo), "query": "chebyshev inequality", "limit": 10}, repo)
        require(any(r["qualified_name"] == "HighDimProb.chebyshev_inequality" for r in theorems["results"]), "theorem search Chebyshev", theorems)

        card_result = mod.theorem_card_tool({"repo_path": str(repo), "qualified_name": "HighDimProb.markov_inequality"}, repo)
        card = card_result.get("card", {})
        require(card.get("label") == "Theorem" and card.get("file_path") == "HighDimProb/Concentration/Markov.lean", "Markov theorem card", card_result)

        context = mod.get_context({"repo_path": str(repo), "qualified_name": "HighDimProb.chebyshev_inequality", "before": 2, "after": 2}, repo)
        require(context.get("found") and context.get("file_path") == "HighDimProb/Concentration/Chebyshev.lean", "Chebyshev context", context)

        code = mod.search_code({"repo_path": str(repo), "pattern": "scaledRandomMatrix", "limit": 10}, repo)
        require(any(r.get("qualified_name") == "HighDimProb.scaledRandomMatrix" for r in code["results"]), "random matrix code search", code)

        debug = mod.debug_parse_file({"repo_path": str(repo), "file": "HighDimProb/Concentration/Markov.lean", "pattern": "markov_inequality", "decl_limit": 10}, repo)
        scanner_names = {item.get("name") for item in debug["scanner_declarations"]}
        require("markov_inequality" in scanner_names, "Markov parser diagnostics", debug)

        missing = mod.get_context({"repo_path": str(repo), "qualified_name": "HighDimProb.thisDeclarationDoesNotExist"}, repo)
        require(not missing.get("found", False), "missing declaration result", missing)

        summary = {
            "release": "alpha-0.1",
            "repo_path": str(repo),
            "index_root": str(index_dir),
            "full_index": full,
            "incremental_index": incremental,
            "checks": [
                "baseline_counts", "incremental_no_op", "search_graph", "search_theorems",
                "theorem_card", "get_context", "search_code", "debug_parse_file", "missing_declaration",
            ],
        }
        return summary
    finally:
        if previous_index_root is None:
            os.environ.pop("LEAN_SEARCH_INDEX_ROOT", None)
        else:
            os.environ["LEAN_SEARCH_INDEX_ROOT"] = previous_index_root
        if owns_index_dir and not keep_index:
            shutil.rmtree(index_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Validate lean-local-search against a pinned HighDimProb release")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--keep-index", action="store_true")
    args = parser.parse_args()
    repo, action = prepare_fixture(args.cache_dir)
    print(f"Fixture {action}: {repo}")
    summary = validate_release(repo, keep_index=args.keep_index)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
